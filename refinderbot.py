import telebot
import yt_dlp
import os
import re
import logging
import json
import sqlite3
from datetime import datetime
import time
from telebot import types


####НАСТРОЙКИ БОТА
ADMIN_USER_ID = os.environ.get('ADMIN_USER_ID', '123456789')  #ID администратора для рассылки сообщений (из переменной окружения)
DATABASE_FILE = os.environ.get('DATABASE_FILE', '/tmp/telemusic.db')  #путь к файлу базы данных
CACHE_DB_FILE = os.environ.get('CACHE_DB_FILE', '/tmp/telegram_bot_cache.json')  #путь к файлу кэша
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/tmp/downloads/")  #папка для загруженных файлов
CACHE_LIFETIME = 60*60*24*30*6  #время жизни кэша результатов поиска (в секундах, здесь 6 месяцев)
FILE_ROTATE = 50  #максимальное количество хранимых аудиофайлов
MAX_LONG = 720  #максимальная длительность загрузки музыки в секундах (12 минут)(при прямой ссылке)
MAX_TITLE_LENGTH = 40  #максимально допустимая длина заголовка перед усечением или сжатием
DROP_VOWELS = 50  #процент гласных, которые нужно сжать в заголовке.
MAX_RESULTS = 15  #количество возвращаемых результатов поиска («ytsearch10» возвращает первые 10)..
MAX_DURATION = 900  #максимальная продолжительность видео в секундах (15 минут)(при поиске)

BOT_TOKEN = os.environ['BOT_TOKEN']  # Токен Telegram бота (переменная окружения)
# Куки YouTube (в формате Netscape) передаются целиком в переменной YOUTUBE_COOKIES
COOKIES_FILE = os.environ.get('COOKIES_FILE', '/tmp/youtube_cookies.txt')
if os.environ.get('YOUTUBE_COOKIES'):
    with open(COOKIES_FILE, 'w', encoding='utf-8') as _f:
        _f.write(os.environ['YOUTUBE_COOKIES'].replace('\\n', '\n'))
YDL_COOKIES = {'cookiefile': COOKIES_FILE} if os.path.exists(COOKIES_FILE) else {}
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
bot = telebot.TeleBot(BOT_TOKEN, threaded=False)  # webhook на Vercel: обрабатываем update синхронно
logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=logging.DEBUG)
cached_search_results = {}
downloaded_files_cache = {}


##БД
# Подключаемся к базе данных 
conn = sqlite3.connect(DATABASE_FILE)
cursor = conn.cursor()
# Создаем таблицу пользователей
cursor.execute('''
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    status TEXT DEFAULT NULL
)
''')

# Создаем таблицу истории запросов
cursor.execute('''
CREATE TABLE IF NOT EXISTS search_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    keywords TEXT NOT NULL,
    last_call TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, keywords),
    FOREIGN KEY(user_id) REFERENCES users(id)
)
''')

# Создаем таблицу истории загрузок
cursor.execute('''
CREATE TABLE IF NOT EXISTS download_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    video_url TEXT NOT NULL,
    filename TEXT NOT NULL,
    UNIQUE(user_id, video_url),
    FOREIGN KEY(user_id) REFERENCES users(id)
)
''')

conn.commit()  # Сохраняем изменения
conn.close()   # Закрываем соединение с базой данных


def db_connect():
    return sqlite3.connect(DATABASE_FILE) 
    
#добавляем пользователя в базу данных
def db_add_user(user_id, username, status=None):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO users (id, username, status) VALUES (?, ?, ?)', (user_id, username, status))
    conn.commit()
    conn.close()
    
#добавляем запись в историю поиска
def db_add_search(user_id, username, keywords):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute('''
    INSERT INTO search_history (user_id, username, keywords, last_call)
    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
    ON CONFLICT(user_id, keywords) DO UPDATE SET last_call = excluded.last_call
    ''', (user_id, username, keywords))
    conn.commit()
    conn.close()
    
#добавляем запись в историю загрузок
def db_add_download(user_id, username, video_url, filename):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute('''
    INSERT INTO download_history (user_id, username, video_url, filename)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(user_id, video_url) DO UPDATE SET filename = excluded.filename
    ''', (user_id, username, video_url, filename))
    conn.commit()
    conn.close()

#объявления, chat_id из базы данных
def load_chat_ids():
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM users')
    chat_ids = [row[0] for row in cursor.fetchall()]
    conn.close()
    return chat_ids


##КЕШ ПОИСКА
#для подгрузки найденого
def load_cache():
    try:
        with open(CACHE_DB_FILE, 'r') as f:
            data = json.load(f)
            cached_search_results.update(data.get('search_results', {}))
            downloaded_files_cache.update(data.get('files', {}))
    except FileNotFoundError:
        logging.info(f"Файл кэша {CACHE_DB_FILE} отсутствует и будет создан новый.")
    except json.JSONDecodeError as e:
        logging.error(f"Ошибка декодирования JSON из файла кэша: {e}")

def save_cache():
    with open(CACHE_DB_FILE, 'w') as f:
        files_data = {url: {'file_path': data['file_path']} for url, data in downloaded_files_cache.items()}
        
        json.dump({
            'search_results': cached_search_results,
            'files': files_data
        }, f, indent=4)
# Загрузка кэша при старте бота
load_cache()

# Получаем результаты из кэша, если они не устарели
def get_cached_search_results(keywords):
    cur_time = time.time()
    cached = cached_search_results.get(keywords, None)
    if cached and cur_time - cached['timestamp'] < CACHE_LIFETIME:  # Проверяем возраст кэша
        return cached['results']
    else:
        return None
        
# Сохраняем результаты и временную метку в кэш
def cache_search_results(keywords, results):
    cached_search_results[keywords] = {
        'timestamp': time.time(),
        'results': results
    }
    save_cache()  # Сохраняем кэш при каждом его обновлении
    
def get_cached_file_path(url):
    cached = downloaded_files_cache.get(url, None)
    if cached:
        return cached['file_path'], url
    return None, url
    
# Сохраняем путь к файлу и URL в кэш
def cache_file_path(url, file_path, download_url):
    downloaded_files_cache[url] = {
        'file_path': file_path,
        'download_url': download_url
    }
    save_cache()  # Сохраняем кэш при каждом его обновлении
    
    
##ФАЙЛЫ
#следим за скаченными файлами
def rotate_files(directory, max_files=FILE_ROTATE):
    files = [file for file in os.listdir(directory) if file.endswith('.mp3')]
    while len(files) > max_files:
        oldest_file = min(files, key=lambda x: os.path.getctime(os.path.join(directory, x)))
        os.remove(os.path.join(directory, oldest_file))
        files.remove(oldest_file)
        logging.info(f'Удален старый файл: {oldest_file}')

#сохраняем
def safe_filename(filename):
    safe_filename = re.sub(r'[\\/*?:"<>|]', "", filename)
    safe_filename = re.sub(r'\s+', " ", safe_filename).strip()
    return safe_filename


##РАБОТА С ТЕКСТОМ
# Функция для проверки запроса на URL YT
def is_youtube_url(url):
    youtube_regex = (
        r'(https?://)?(www\.)?'
        '(youtube|youtu|youtube-nocookie)\.(com|be)/'
        '(watch\?v=|embed/|v/|.+\?v=)?([^&=%\?]{11})')
    youtube_match = re.match(youtube_regex, url)
    return bool(youtube_match)
   
#Будем ломать слова для Саши(сокращение текста в кнопках)
def compress_title(title, vowel_pct=DROP_VOWELS):
    vowels = 'aeiouAEIOUаеёиоуыэюя'# Определяем гласные буквы
    words = title.split() # Разделяем название на слова
    new_words = [] 
    for word in words: # Проходимся по каждому слову
        if len(word) <= 4: # Если слово короткое, оставляем как есть
            new_words.append(word)
            continue
        num_vowels_to_compress = len([c for c in word[1:-1] if c in vowels]) * vowel_pct // 100 # Подсчитываем количество гласных к удалению
        compressed_word = []
        vowels_count = 0
        compressed_word.append(word[0]) # Копируем первую и последнюю букву       
        for char in word[1:-1]: # Удаляем гласные из середины слова
            if vowels_count < num_vowels_to_compress and char in vowels:
                vowels_count += 1
                continue
            compressed_word.append(char)
        compressed_word.append(word[-1]) # Копируем последнюю букву
        new_words.append(''.join(compressed_word)) # Возвращаем сжатое слово
    return ' '.join(new_words) # Объединяем слова обратно в строку

def trim_or_compress_title(title, max_length=MAX_TITLE_LENGTH):
    if len(title) <= max_length:
        return title
    return compress_title(title)[:max_length-3] + '...'
    
#наверное можно сразу из либы доставать, но потом доку почитаю
def format_duration(seconds):
    minutes, sec = divmod(seconds, 60)
    return f"{minutes:02d}:{sec:02d}"
    
#изолируем
def sanitize(input_string):
    return re.sub(r'[^\w\s,.!?-]', '', input_string)
    
    
##ПОИСК И ЗАГРУЗКА МУЗЫКИ
#поиск трека
def search_music(username, keywords, search_amount=MAX_RESULTS):
    logging.info(f'Начинаем поиск музыки по ключевым словам: {keywords}')
    cached_results = get_cached_search_results(keywords)
    if cached_results is not None:
        return cached_results
    ydl_opts = {
        'default_search': f'ytsearch{search_amount}',
        'noplaylist': True,
        'quiet': True,
        **YDL_COOKIES
        }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(keywords, download=False)
        if not info or 'entries' not in info or not info['entries']:
            logging.error('Ничего не найдено.')
            return None
        else:
            logging.info('Нашли музыку по запросу: ' + info['entries'][0]['webpage_url'])
            filtered_entries = [
        {
            'id': e.get('id'),
            'title': e.get('title', 'Unknown Title'),
            'webpage_url': e.get('webpage_url'),
            'duration': e.get('duration', 0),
        }
        for e in info['entries'] if e.get('duration', 0) <= 900
    ]
    cache_search_results(keywords, filtered_entries)
    return filtered_entries
    
#загрузка трека (без ffmpeg: берём готовый m4a/mp3 от YouTube)
def download_and_convert_music(user_id, url):
    logging.info(f'def download_and_convert_music {url}')
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    rotate_files(DOWNLOAD_DIR)
    cached_file_path, cached_url = get_cached_file_path(url)
    if cached_file_path and os.path.exists(cached_file_path):
        return cached_file_path
    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio',
        **YDL_COOKIES,
        'quiet': True,
        'outtmpl': os.path.join(DOWNLOAD_DIR, '%(id)s.%(ext)s'),
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        logging.info(f'Музыка скачана по URL: {url}')
        temp_file_path = ydl.prepare_filename(info)
        ext = os.path.splitext(temp_file_path)[1]
        title = safe_filename(info.get('title', 'Unknown Title'))
        new_file_path = os.path.join(DOWNLOAD_DIR, f'{title}{ext}')
        if not os.path.exists(temp_file_path):
            logging.error(f'Скачанный файл {temp_file_path} не найден.')
            return None
        os.replace(temp_file_path, new_file_path)
        cache_file_path(url, new_file_path, url)
        logging.info(f'Файл успешно сохранен как {new_file_path}')
        return new_file_path


###БОТ И ДЕЙСТВИЯ
def send_welcome(chat_id):
    welcome_text = 'Привет! Для поиска музыки введите название трека.'
    bot.send_message(chat_id, welcome_text)
    
#старт:
@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    username = message.from_user.username
    chat_id = message.chat.id
    keywords = message.text
    db_add_user(user_id, username)
    # Приветственное сообщение для пользователя при активации бота командой /start
    send_welcome(chat_id)

##OVERDRIVE
#объявления
@bot.message_handler(commands=['broadcast'])
def handle_broadcast_command(message):
    if str(message.from_user.id) == ADMIN_USER_ID:
        args = message.text.split(maxsplit=1)  # Разбиваем сообщение на части
        if len(args) > 1:
            # Вторая часть - это текст сообщения, который нужно отправить
            text = args[1]
            broadcast_message(text)
            bot.reply_to(message, "Сообщение отправлено всем пользователям.")
        else:
            bot.reply_to(message, "Пожалуйста, укажите текст сообщения после команды.")
    else:
        bot.reply_to(message, "У вас нет прав использовать эту команду.")

def broadcast_message(text):
    chat_ids = load_chat_ids()
    for chat_id in chat_ids:
        try:
            bot.send_message(chat_id, text)
        except Exception as e:
            logging.error(f"Не удалось отправить сообщение пользователю {chat_id}: {e}")
#    
#запрос
@bot.message_handler(content_types=['text'])
def text(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    username = message.from_user.username 
    keywords = sanitize(message.text)
    db_add_search(user_id, username, keywords)
    # Проверям, является ли текст сообщения ссылкой на YouTube
    if is_youtube_url(keywords):
        # Проверяем длительность видео перед скачиванием
        ydl_opts = {
            'format': 'bestaudio/best',
            'quiet': True,
            **YDL_COOKIES,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(keywords, download=False)
                video_duration = info.get('duration', 0)  # продолжительность в секундах
            except yt_dlp.utils.DownloadError:
                # Если не удалось получить информацию о видео
                bot.send_message(chat_id, "Не удалось получить информацию о видео.")
                return
        if video_duration > 720:
            # Если видео длиннее 12 минут
            bot.send_message(chat_id, "Видео длиннее 12 минут и не может быть обработано.")
        else:
            # Если видео подходит по длительности, скачиваем и конвертируем
            file_path = download_and_convert_music(username, keywords)
            if file_path:
                # Если файл успешно скачан, отправляем музыку
                send_music(chat_id, file_path)
            else:
                # Если возникла ошибка при скачивании
                bot.send_message(chat_id, "Не удалось скачать трек.")
    else:
        # Обрабатываем остальной текст как поиск
        results = search_music(username, keywords)
        if results:
            send_results(chat_id, results)
        else:
            bot.send_message(chat_id, "К сожалению, ничего не найдено. Попробуйте другой запрос.")  
    
#отправка результатов поиска (все результаты одним сообщением; id видео кладём в callback_data)
def send_results(chat_id, results):
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for i, item in enumerate(results):
        if not item.get('id'):
            continue
        duration = format_duration(item['duration']) if item.get('duration') else 'Unknown'
        title = trim_or_compress_title(item.get('title', 'Unknown Title'))
        button_text = f"{i + 1}. {title} [{duration}]"
        keyboard.add(types.InlineKeyboardButton(button_text, callback_data=f"dl:{item['id']}"))
    bot.send_message(chat_id, "Выберите трек для скачивания:", reply_markup=keyboard)

#загрузка по нажатию кнопки
@bot.callback_query_handler(func=lambda call: call.data.startswith('dl:'))
def callback_query(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    username = call.from_user.username
    url = f"https://www.youtube.com/watch?v={call.data[3:]}"
    bot.answer_callback_query(call.id)
    file_path = download_and_convert_music(user_id, url)
    if file_path:
        db_add_download(user_id, username, url, file_path)
        send_music(chat_id, file_path)
    else:
        bot.send_message(chat_id, "Не удалось скачать трек.")

def send_music(chat_id, filename):
    logging.info(f'Начинаем отправку музыки в чат {chat_id} по имени файла: {filename}')
    with open(filename, 'rb') as audio:
        if filename.lower().endswith(('.mp3', '.m4a')):
            bot.send_audio(chat_id, audio)
        else:
            bot.send_document(chat_id, audio)
    logging.info(f'Отправили музыку в чат {chat_id} по имени файла: {filename}')

# Запуск (polling) выполняется через webhook: см. api/index.py
