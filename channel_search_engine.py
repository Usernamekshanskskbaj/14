import asyncio
import logging
import time
import random
import threading
import os  # Добавляем этот импорт
from datetime import datetime
from typing import List, Tuple, Set, Optional
import re

# Настройка логирования
log_filename = 'run_log.log'

# Проверяем, нужно ли создавать новый файл
file_mode = 'w' if not os.path.exists(log_filename) else 'a'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_filename, mode=file_mode, encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# Создаем логгер после настройки logging
logger = logging.getLogger(__name__)

try:
    from seleniumbase import Driver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.common.keys import Keys
    from selenium.common.exceptions import TimeoutException, WebDriverException
    from telethon import TelegramClient
    from telethon.errors import ChannelPrivateError, ChannelInvalidError, FloodWaitError
    from telethon.tl.functions.channels import GetFullChannelRequest
    import g4f
except ImportError as e:
    logger.error(f"Ошибка импорта библиотек: {e}")
    raise

# Глобальные переменные
search_active = False
found_channels: Set[str] = set()
driver = None
current_settings = {}
shared_telethon_client = None
search_progress = {'current_keyword': '', 'current_topic': ''}

# Переменные для контроля flood wait
last_api_call_time = 0
api_call_interval = 1.0  # Минимальный интервал между API вызовами (секунды)
flood_wait_delays = {}  # Словарь для хранения времени ожидания для разных операций

async def handle_flood_wait(func, *args, **kwargs):
    """
    Универсальная обертка для обработки FloodWaitError
    """
    max_retries = 3
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            # Проверяем минимальный интервал между API вызовами
            global last_api_call_time, api_call_interval
            current_time = time.time()
            time_since_last_call = current_time - last_api_call_time
            
            if time_since_last_call < api_call_interval:
                wait_time = api_call_interval - time_since_last_call
                await asyncio.sleep(wait_time)
            
            last_api_call_time = time.time()
            
            # Выполняем функцию
            if asyncio.iscoroutinefunction(func):
                result = await func(*args, **kwargs)
            else:
                result = func(*args, **kwargs)
            
            return result
            
        except FloodWaitError as e:
            retry_count += 1
            wait_seconds = e.seconds
            
            logger.warning(f"FloodWaitError: необходимо подождать {wait_seconds} секунд. Попытка {retry_count}/{max_retries}")
            
            # Увеличиваем интервал между вызовами для предотвращения повторных flood wait
            api_call_interval = min(api_call_interval * 1.5, 10.0)
            
            if retry_count >= max_retries:
                logger.error(f"Достигнуто максимальное количество попыток для обработки FloodWait. Последнее ожидание: {wait_seconds} секунд")
                raise
            
            # Проверяем, не запрошена ли остановка во время ожидания
            try:
                import bot_interface
                if not bot_interface.bot_data.get('is_running', True):
                    logger.info("Остановка запрошена во время FloodWait, прерываем ожидание")
                    raise asyncio.CancelledError("Остановка запрошена")
            except ImportError:
                pass
            
            # Ждем с проверкой состояния каждые 10 секунд
            remaining_wait = wait_seconds
            while remaining_wait > 0:
                # Проверяем состояние каждые 10 секунд или оставшееся время, если оно меньше
                check_interval = min(10, remaining_wait)
                await asyncio.sleep(check_interval)
                remaining_wait -= check_interval
                
                # Проверяем, не запрошена ли остановка
                try:
                    import bot_interface
                    if not bot_interface.bot_data.get('is_running', True):
                        logger.info("Остановка запрошена во время FloodWait, прерываем ожидание")
                        raise asyncio.CancelledError("Остановка запрошена")
                except ImportError:
                    pass
                
                if remaining_wait > 0:
                    logger.info(f"FloodWait: осталось ждать {remaining_wait} секунд")
            
            logger.info(f"Ожидание FloodWait завершено, повторяем попытку {retry_count + 1}")
            
        except Exception as e:
            # Если это не FloodWaitError, просто пробрасываем исключение
            raise e
    
    # Если мы дошли до сюда, значит все попытки исчерпаны
    raise Exception(f"Не удалось выполнить операцию после {max_retries} попыток")

def setup_driver():
    """Настройка и инициализация веб-драйвера"""
    logger.info("Инициализация веб-драйвера...")
    
    try:
        # Создаем драйвер
        driver = Driver(uc=True, headless=False)
        driver.set_window_size(600, 1200)
        
        # Установка десктопного user-agent
        desktop_user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        driver.execute_cdp_cmd('Network.setUserAgentOverride', {"userAgent": desktop_user_agent})
        
        # Проверка работоспособности драйвера
        driver.get("about:blank")
        logger.info("Веб-драйвер успешно инициализирован")
        
        return driver
    except Exception as e:
        logger.error(f"Ошибка инициализации драйвера: {e}")
        return None

def wait_and_find_element(driver, selectors, timeout=5):
    """Поиск элемента по нескольким селекторам"""
    if isinstance(selectors, str):
        selectors = [selectors]
    
    for selector in selectors:
        try:
            if selector.startswith('//'):
                element = WebDriverWait(driver, timeout).until(
                    EC.presence_of_element_located((By.XPATH, selector))
                )
            else:
                element = WebDriverWait(driver, timeout).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                )
            return element
        except TimeoutException:
            continue
        except Exception as e:
            logger.warning(f"Ошибка поиска элемента {selector}: {e}")
            continue
    
    return None

def wait_and_click_element(driver, selectors, timeout=5):
    """Клик по элементу с ожиданием его доступности"""
    element = wait_and_find_element(driver, selectors, timeout)
    if element:
        try:
            if isinstance(selectors, str):
                selectors = [selectors]
            
            for selector in selectors:
                try:
                    if selector.startswith('//'):
                        clickable_element = driver.find_element(By.XPATH, selector)
                    else:
                        clickable_element = driver.find_element(By.CSS_SELECTOR, selector)
                    
                    if clickable_element.is_displayed() and clickable_element.is_enabled():
                        clickable_element.click()
                        return True
                    else:
                        time.sleep(0.1)
                        if clickable_element.is_displayed() and clickable_element.is_enabled():
                            clickable_element.click()
                            return True
                except:
                    continue
        except Exception as e:
            logger.warning(f"Ошибка клика по элементу: {e}")
    
    return False

def navigate_to_channel_search(driver):
    """Навигация к странице поиска каналов"""
    try:
        logger.info("Переход на tgstat.ru...")
        driver.get("https://tgstat.ru/")
        time.sleep(1)
        
        # Проверяем состояние is_running
        try:
            import bot_interface
            if not bot_interface.bot_data['is_running']:
                logger.info("Остановка запрошена, прерываем навигацию")
                return False
        except:
            pass
        
        # Нажимаем на три полоски для открытия меню
        logger.info("Открываем меню...")
        menu_selectors = [
            'a.d-flex.d-lg-none.nav-user',
            '.nav-user',
            '[data-toggle="collapse"]',
            'i.uil-bars'
        ]
        
        if not wait_and_click_element(driver, menu_selectors, 3):
            logger.warning("Не удалось найти кнопку меню, возможно меню уже открыто")
        
        time.sleep(0.5)
        
        # Проверяем состояние is_running
        try:
            import bot_interface
            if not bot_interface.bot_data['is_running']:
                logger.info("Остановка запрошена, прерываем навигацию")
                return False
        except:
            pass
        
        # Открываем выпадающее меню "Каталог"
        logger.info("Открываем каталог...")
        catalog_selectors = [
            '#topnav-catalog',
            'a[id="topnav-catalog"]',
            '.nav-link.dropdown-toggle',
            '//a[contains(text(), "Каталог")]'
        ]
        
        if not wait_and_click_element(driver, catalog_selectors, 3):
            logger.error("Не удалось найти кнопку каталога")
            return False
        
        time.sleep(0.3)
        
        # Проверяем состояние is_running
        try:
            import bot_interface
            if not bot_interface.bot_data['is_running']:
                logger.info("Остановка запрошена, прерываем навигацию")
                return False
        except:
            pass
        
        # Нажимаем на "Поиск каналов"
        logger.info("Переходим к поиску каналов...")
        search_selectors = [
            'a[href="/channels/search"]',
            '//a[contains(text(), "Поиск каналов")]',
            '.dropdown-item[href="/channels/search"]'
        ]
        
        if not wait_and_click_element(driver, search_selectors, 3):
            logger.error("Не удалось найти кнопку поиска каналов")
            return False
        
        time.sleep(1)
        logger.info("Успешно перешли на страницу поиска каналов")
        return True
        
    except Exception as e:
        logger.error(f"Ошибка навигации: {e}")
        return False

def search_channels_sync(driver, keyword: str, topic: str, first_search: bool = False):
    """Функция поиска каналов"""
    try:
        # Проверяем состояние is_running
        try:
            import bot_interface
            if not bot_interface.bot_data['is_running']:
                logger.info("Остановка запрошена, прерываем поиск")
                return []
        except:
            pass
        
        logger.info(f"Поиск каналов по ключевому слову: '{keyword}', тема: '{topic}'")
        
        # Вводим ключевое слово
        keyword_input = wait_and_find_element(driver, [
            'input[name="q"]',
            '#q',
            '.form-control',
            'input[placeholder*="канал"]',
            'input.form-control'
        ])
        
        if keyword_input:
            keyword_input.clear()
            time.sleep(0.1)
            keyword_input.send_keys(keyword)
            logger.info(f"Введено ключевое слово: {keyword}")
        else:
            logger.error("Не удалось найти поле ввода ключевого слова")
            return []
        
        # Проверяем состояние is_running
        try:
            import bot_interface
            if not bot_interface.bot_data['is_running']:
                logger.info("Остановка запрошена, прерываем поиск")
                return []
        except:
            pass
        
        # Вводим тему
        topic_input = wait_and_find_element(driver, [
            '.select2-search__field',
            'input[role="searchbox"]',
            '.select2-search input'
        ])
        
        if topic_input:
            topic_input.clear()
            time.sleep(0.1)
            topic_input.send_keys(topic)
            time.sleep(0.3)
            topic_input.send_keys(Keys.ENTER)
            logger.info(f"Введена тема: {topic}")
        else:
            logger.error("Не удалось найти поле ввода темы")
            return []
        
        # Проверяем состояние is_running
        try:
            import bot_interface
            if not bot_interface.bot_data['is_running']:
                logger.info("Остановка запрошена, прерываем поиск")
                return []
        except:
            pass
        
        # Если это первый поиск, настраиваем дополнительные опции
        if first_search:
            # Отмечаем "также искать в описании"
            description_checkbox = wait_and_find_element(driver, [
                '#inabout',
                'input[name="inAbout"]',
                '.custom-control-input[name="inAbout"]'
            ])
            
            if description_checkbox and not description_checkbox.is_selected():
                driver.execute_script("arguments[0].click();", description_checkbox)
                logger.info("Отмечен поиск в описании")
            
            # Выбираем тип канала "публичный"
            channel_type_select = wait_and_find_element(driver, [
                '#channeltype',
                'select[name="channelType"]',
                '.custom-select[name="channelType"]'
            ])
            
            if channel_type_select:
                driver.execute_script("arguments[0].value = 'public';", channel_type_select)
                logger.info("Выбран тип канала: публичный")
        
        # Проверяем состояние is_running перед поиском
        try:
            import bot_interface
            if not bot_interface.bot_data['is_running']:
                logger.info("Остановка запрошена, прерываем поиск")
                return []
        except:
            pass
        
        # Нажимаем кнопку "Искать"
        search_button = wait_and_find_element(driver, [
            '#search-form-submit-btn',
            'button[type="button"].btn-primary',
            '.btn.btn-primary.w-100'
        ])
        
        if search_button:
            driver.execute_script("arguments[0].click();", search_button)
            logger.info("Нажата кнопка поиска")
        else:
            logger.error("Не удалось найти кнопку поиска")
            return []
       
        # Ждем загрузки результатов с проверкой состояния
        wait_time = 0
        max_wait = 10
        while wait_time < max_wait:
            # Проверяем состояние is_running каждую секунду
            try:
                import bot_interface
                if not bot_interface.bot_data['is_running']:
                    logger.info("Остановка запрошена, прерываем ожидание результатов")
                    return []
            except:
                pass
            
            time.sleep(1)
            wait_time += 1
            
            # Проверяем, загрузились ли результаты
            try:
                results = driver.find_elements(By.CSS_SELECTOR, '.card.peer-item-row, .peer-item-row')
                if results:
                    break
            except:
                continue
        
        # Извлекаем результаты
        channels = extract_channel_usernames_sync(driver)
        logger.info(f"Найдено каналов: {len(channels)}")
        
        return channels
        
    except Exception as e:
        logger.error(f"Ошибка поиска каналов: {e}")
        return []

def extract_channel_usernames_sync(driver) -> List[str]:
    """Функция извлечения юзернеймов каналов"""
    usernames = []
    
    try:
        # Проверяем состояние is_running
        try:
            import bot_interface
            if not bot_interface.bot_data['is_running']:
                logger.info("Остановка запрошена, прерываем извлечение каналов")
                return usernames
        except:
            pass
        
        # Ждем появления результатов
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, '.card.peer-item-row, .peer-item-row'))
        )
        
        # Ищем все карточки каналов
        channel_cards = driver.find_elements(By.CSS_SELECTOR, '.card.peer-item-row, .peer-item-row')
        
        if not channel_cards:
            logger.warning("Не найдено карточек каналов")
            return usernames
        
        for card in channel_cards:
            try:
                # Проверяем состояние is_running для каждого канала
                try:
                    import bot_interface
                    if not bot_interface.bot_data['is_running']:
                        logger.info("Остановка запрошена, прерываем обработку каналов")
                        break
                except:
                    pass
                
                # Ищем ссылку на канал
                link_elements = card.find_elements(By.CSS_SELECTOR, 'a[href*="/channel/@"]')
                
                for link in link_elements:
                    href = link.get_attribute('href')
                    if href and '/channel/@' in href:
                        # Извлекаем username из ссылки
                        match = re.search(r'/channel/(@[^/]+)', href)
                        if match:
                            username = match.group(1)
                            if username not in usernames:
                                usernames.append(username)
                                logger.info(f"Найден канал: {username}")
                                break
                
            except Exception as e:
                logger.warning(f"Ошибка обработки карточки канала: {e}")
                continue
        
        return usernames
        
    except TimeoutException:
        logger.warning("Результаты поиска не загрузились за отведенное время")
        return usernames
    except Exception as e:
        logger.error(f"Ошибка извлечения юзернеймов: {e}")
        return usernames

async def check_channel_comments_available(client: TelegramClient, username: str) -> bool:
    """Проверка доступности комментариев в канале с использованием GetFullChannelRequest и обработкой FloodWait"""
    try:
        if not username.startswith('@'):
            username = '@' + username
        
        # Используем обертку для обработки FloodWait
        async def get_entity_safe():
            return await client.get_entity(username)
        
        async def get_full_channel_safe(entity):
            return await client(GetFullChannelRequest(entity))
        
        # Получаем информацию о канале с обработкой FloodWait
        entity = await handle_flood_wait(get_entity_safe)
        
        # Получаем полную информацию о канале с обработкой FloodWait
        full_channel = await handle_flood_wait(get_full_channel_safe, entity)
        
        # Проверяем, есть ли linked_chat_id
        if hasattr(full_channel.full_chat, 'linked_chat_id') and full_channel.full_chat.linked_chat_id:
            logger.info(f"Канал {username} имеет группу обсуждений (linked_chat_id: {full_channel.full_chat.linked_chat_id})")
            return True
        else:
            logger.info(f"Канал {username} не имеет группы обсуждений")
            return False
        
    except (ChannelPrivateError, ChannelInvalidError):
        logger.warning(f"Канал {username} недоступен")
        return False
    except FloodWaitError as e:
        logger.error(f"FloodWaitError при проверке канала {username}: {e.seconds} секунд")
        raise  # Пробрасываем FloodWaitError для обработки в handle_flood_wait
    except Exception as e:
        logger.warning(f"Ошибка проверки канала {username}: {e}")
        return False

async def analyze_channel(channel_id: int) -> Tuple[List[str], List[str]]:
    """Анализ канала для определения тематики и ключевых слов с обработкой FloodWait (с логированием)"""
    try:
        # Используем общий клиент
        global shared_telethon_client
        if not shared_telethon_client:
            logger.error("Telethon клиент не инициализирован в поисковике")
            return [], []
        
        async def get_entity_safe():
            return await shared_telethon_client.get_entity(channel_id)
        
        async def iter_messages_safe(entity, target_text_posts=30):
            """Получаем именно target_text_posts текстовых постов"""
            text_messages = []
            total_checked = 0
            
            async for message in shared_telethon_client.iter_messages(entity, limit=None):
                total_checked += 1
                
                if message.text and message.text.strip():
                    text_messages.append(message.text.strip())
                    
                    if len(text_messages) >= target_text_posts:
                        break
                
                if total_checked >= 1000:
                    logger.warning(f"Достигнут лимит проверки 1000 сообщений. Найдено {len(text_messages)} текстовых постов")
                    break
            
            logger.info(f"Проверено {total_checked} сообщений, найдено {len(text_messages)} текстовых постов")
            return text_messages
        
        entity = await handle_flood_wait(get_entity_safe)
        
        channel_info = []
        
        if hasattr(entity, 'title'):
            channel_info.append(f"Название: {entity.title}")
        
        if hasattr(entity, 'about') and entity.about:
            channel_info.append(f"Описание: {entity.about}")
        
        posts_text = await handle_flood_wait(iter_messages_safe, entity, 50)
        
        posts_count = len(posts_text) if posts_text else 0
        logger.info(f"Получено текстовых постов для анализа канала {channel_id}: {posts_count}")
        
        if posts_text:
            posts_text.reverse()
            
            formatted_posts = []
            for post in posts_text:
                formatted_posts.append(f"**Пост:**\n\n{post}\n\n——————————————————")
            
            channel_info.append("\n\n".join(formatted_posts))
            
            logger.info(f"В промт добавлено {len(formatted_posts)} постов")
        else:
            channel_info.append("Посты не найдены")
            logger.info("Посты не найдены для добавления в промт")
        
        full_text = "\n".join(channel_info)
        
        prompt = f"""Данные канала:

{full_text}

———————————————————————

Ты — профессиональный аналитик Telegram-каналов. Проанализируй название, описание и посты канала и в результате анализа выдай два списка.

📌 1. Сгенерируй **ТОЧНЫЕ** ключевые слова, которые могли бы встречаться в **названиях других каналов точно по той же тематике**.

- Ключевые слова должны **на 100% отражать основную тему канала**.
- Тема считается основной, если она присутствует в **описании** или явно **доминирует в 90%+ постов по смыслу** (а не просто упоминается).
- **Запрещено использовать любые слова**, которые:
  - связаны с темой **косвенно**;
  - абстрактны ("ощущение", "эмоции", "реализация", "настроение", "стиль", "идея" и подобные);
  - упоминаются в канале **случайно, единично или как пример**.
- **Разрешено использовать только те слова**, которые:
  - короткие и точные;
  - могли бы быть в названии другого Telegram-канала с точно такой же темой;
  - прямо и явно обозначают основную тематику.

📌 2. Определи основную тему или темы канала, строго выбрав их из следующего списка:

["Бизнес и стартапы", "Блоги", "Букмекерство", "Видео и фильмы", "Даркнет", "Дизайн", "Для взрослых", "Еда и кулинария", "Здоровье и медицина", "Игры", "Искусство", "История", "Книги", "Красота и мода", "Криптовалюты", "Лайфхаки", "Маркетинг, PR, реклама", "Музыка", "Наука", "Новости и СМИ", "Образование", "Политика", "Психология", "Путешествия", "Работа и карьера", "Развлечения", "Религия", "Семья и дети", "Спорт", "Технологии", "Фото", "Эзотерика", "Юмор", "Другое"]

- Выбирай только те темы, которые **на 100% соответствуют смыслу канала**.
- **Запрещено выбирать темы, если они связаны только частично или косвенно.**
- Если ни одна тема не подходит точно — укажи "Другое".
- **Запрещено придумывать темы вне списка.**
- **Категорически запрещено выбирать темы которых нет в списке**

🎯 Главные правила:
- Описание канала — **главный ориентир**. Посты нужны только как подтверждение.
- Игнорируй слова и темы, встречающиеся **в одном или нескольких постах**, если они **не повторяются стабильно во всех или большинстве постов**.
- Не используй обобщения, эмоции, художественные слова, метафоры, стили и прочий мусор — **только суть**.

📤 Формат ответа:

ТЕМЫ: укажи только темы из списка. Если тем несколько, то пиши каждую через запятую.
КЛЮЧЕВЫЕ_СЛОВА: только короткие, точные, релевантные слова, для названия канала по этой теме. Каждое слово через запятую.

Отвечай строго в заданном формате. """
        
        try:
            response = await g4f.ChatCompletion.create_async(
                model="gemini-2.5-flash",
                messages=[{"role": "user", "content": prompt}]
            )
            
            print("="*50)
            print("ОТВЕТ ОТ ИИ:")
            print("="*50)
            print(response)
            print("="*50)
            
            topics = []
            keywords = []
            
            lines = response.split('\n')
            for line in lines:
                if line.startswith('ТЕМЫ:'):
                    topics_text = line.replace('ТЕМЫ:', '').strip()
                    topics = [topic.strip() for topic in topics_text.split(',') if topic.strip()]
                elif line.startswith('КЛЮЧЕВЫЕ_СЛОВА:'):
                    keywords_text = line.replace('КЛЮЧЕВЫЕ_СЛОВА:', '').strip()
                    keywords = [kw.strip() for kw in keywords_text.split(',') if kw.strip()]
            
            print(f"Исходные темы от ИИ: {topics}")
            print(f"Исходные ключевые слова от ИИ: {keywords}")
            
            valid_topics = [
                "Бизнес и стартапы", "Блоги", "Букмекерство", "Видео и фильмы", "Даркнет", "Дизайн", 
                "Для взрослых", "Еда и кулинария", "Здоровье и медицина", "Игры", "Искусство", "История", 
                "Книги", "Красота и мода", "Криптовалюты", "Лайфхаки", "Маркетинг, PR, реклама", "Музыка", 
                "Наука", "Новости и СМИ", "Образование", "Политика", "Психология", "Путешествия", 
                "Работа и карьера", "Развлечения", "Религия", "Семья и дети", "Спорт", "Технологии", 
                "Фото", "Эзотерика", "Юмор", "Другое"
            ]
            
            filtered_topics = []
            
            topics_text = ', '.join(topics)
            for valid_topic in valid_topics:
                if topics_text.lower() == valid_topic.lower():
                    filtered_topics.append(valid_topic)
                    break
            
            if not filtered_topics:
                for topic in topics:
                    for valid_topic in valid_topics:
                        if topic.lower() == valid_topic.lower():
                            if valid_topic not in filtered_topics:
                                filtered_topics.append(valid_topic)
                            break
            
            unique_keywords = []
            for keyword in keywords:
                keyword_clean = keyword.strip()
                if keyword_clean and keyword_clean not in unique_keywords:
                    unique_keywords.append(keyword_clean)
            
            if not filtered_topics:
                filtered_topics = ['Другое']
                logger.warning("Не найдено подходящих тем, установлена тема 'Другое'")
            if not unique_keywords:
                unique_keywords = ['общее', 'контент']
                logger.warning("Не найдено ключевых слов, установлены дефолтные")
            
            print(f"Отфильтрованные темы: {filtered_topics}")
            print(f"Уникальные ключевые слова: {unique_keywords}")
            
            logger.info(f"Анализ канала завершен. Темы: {filtered_topics}, Ключевые слова: {unique_keywords}")
            return filtered_topics, unique_keywords
            
        except Exception as e:
            logger.error(f"Ошибка анализа с GPT-4: {e}")
            return ['Бизнес и стартапы', 'Маркетинг, PR, реклама'], ['бизнес', 'маркетинг', 'продвижение']
    
    except FloodWaitError as e:
        logger.error(f"FloodWaitError при анализе канала: {e.seconds} секунд")
        raise
    except Exception as e:
        logger.error(f"Ошибка анализа канала: {e}")
        return [], []

async def process_found_channels(channels: List[str]):
    """Обработка найденных каналов с обработкой FloodWait и обновлением статистики"""
    # Используем общий клиент
    global shared_telethon_client
    if not shared_telethon_client:
        logger.error("Telethon клиент не инициализирован в поисковике")
        return
    
    logger.info(f"Начинаем обработку {len(channels)} найденных каналов")
    
    for i, username in enumerate(channels, 1):
        # Проверяем состояние is_running перед обработкой каждого канала
        try:
            import bot_interface
            if not bot_interface.bot_data['is_running']:
                logger.info("Остановка запрошена, прерываем обработку каналов")
                break
        except:
            pass
        
        if username in found_channels:
            logger.info(f"Канал {username} уже был обработан ранее, пропускаем")
            continue  # Канал уже был обработан
        
        logger.info(f"Проверяем канал {i}/{len(channels)}: {username}")
        
        try:
            # Проверяем доступность комментариев с использованием GetFullChannelRequest и обработкой FloodWait
            comments_available = await handle_flood_wait(check_channel_comments_available, shared_telethon_client, username)
            
            if comments_available:
                logger.info(f"Канал {username} доступен для комментариев")
                found_channels.add(username)
                
                # Обновляем статистику найденных каналов в bot_interface
                try:
                    bot_interface.update_found_channels_statistics(list(found_channels))
                except Exception as e:
                    logger.error(f"Ошибка обновления статистики найденных каналов: {e}")
                
                # Передаем канал в masslooker
                try:
                    import masslooker
                    await masslooker.add_channel_to_queue(username)
                    logger.info(f"Канал {username} добавлен в очередь масслукера")
                except Exception as e:
                    logger.error(f"Ошибка добавления канала в очередь: {e}")
            else:
                logger.info(f"Канал {username} недоступен для комментариев")
        
        except FloodWaitError as e:
            logger.warning(f"FloodWaitError при обработке канала {username}: {e.seconds} секунд. Пропускаем канал.")
            continue
        except Exception as e:
            logger.error(f"Ошибка обработки канала {username}: {e}")
        
        # Увеличенная задержка между проверками каналов для избежания FloodWait
        await asyncio.sleep(random.uniform(2.0, 4.0))
    
    logger.info(f"Завершена обработка каналов. Найдено подходящих: {len([ch for ch in channels if ch in found_channels])}")

async def save_search_progress():
    """Сохранение прогресса поиска в базу данных"""
    try:
        from database import db
        # Сохраняем прогресс пакетно для лучшей производительности
        progress_data = [
            ('search_progress', search_progress),
            ('found_channels', list(found_channels))
        ]
        
        for key, value in progress_data:
            await db.save_bot_state(key, value)
    except Exception as e:
        logger.error(f"Ошибка сохранения прогресса поиска: {e}")

async def load_search_progress():
    """Загрузка прогресса поиска из базы данных"""
    global search_progress, found_channels
    try:
        from database import db
        
        # Загружаем прогресс поиска
        saved_progress = await db.load_bot_state('search_progress', {})
        if saved_progress:
            search_progress.update(saved_progress)
        
        # Загружаем найденные каналы
        saved_channels = await db.load_bot_state('found_channels', [])
        if saved_channels:
            found_channels.update(saved_channels)
        
        logger.info(f"Загружен прогресс поиска: {search_progress}")
        logger.info(f"Загружено найденных каналов: {len(found_channels)}")
    except Exception as e:
        logger.error(f"Ошибка загрузки прогресса поиска: {e}")

def search_loop_threaded():
    """УДАЛЕНО: функция создавала новый event loop, что вызывало конфликты с TelethonClient"""
    logger.error("search_loop_threaded больше не используется. Используйте start_search_in_main_loop() вместо этого.")
    pass

async def search_loop_async():
    """Основная функция поиска каналов"""
    global driver, search_active
    
    logger.info("🔍 Начинаем основной цикл поиска каналов")
    
    # Загружаем прогресс поиска при запуске
    await load_search_progress()
    
    logger.info(f"📊 Начальное состояние: search_active={search_active}")
    
    while search_active:
        try:
            # Проверяем состояние is_running в начале каждой итерации
            try:
                import bot_interface
                bot_is_running = bot_interface.bot_data.get('is_running', True)
                logger.debug(f"🤖 Состояние bot_interface.is_running: {bot_is_running}")
                
                if not bot_is_running:
                    logger.info("Остановка запрошена в bot_interface, завершаем поиск")
                    search_active = False
                    break
            except Exception as e:
                logger.warning(f"⚠️ Не удалось проверить состояние bot_interface (возможно тест): {e}")
                # В режиме тестирования продолжаем работу
            
            # Проверяем наличие настоящего Telethon клиента
            if not shared_telethon_client:
                logger.error("❌ Нет Telethon клиента, поиск невозможен")
                search_active = False
                break
            
            # Проверяем, это мок-клиент или настоящий
            if hasattr(shared_telethon_client, 'connected') and not hasattr(shared_telethon_client, 'api_id'):
                logger.info("🧪 Обнаружен мок-клиент, завершаем тестовый поиск")
                search_active = False
                break
            
            # Инициализируем драйвер
            if not driver:
                logger.info("🚀 Инициализируем веб-драйвер для поиска...")
                driver = setup_driver()
                if not driver:
                    logger.error("❌ Не удалось инициализировать драйвер")
                    await asyncio.sleep(60)
                    continue
            
            # Навигация к поиску каналов
            if not navigate_to_channel_search(driver):
                logger.error("Не удалось перейти к поиску каналов")
                await asyncio.sleep(60)
                continue
            
            keywords = current_settings.get('keywords', [])
            topics = current_settings.get('topics', [])
            
            if not keywords or not topics:
                logger.warning("Ключевые слова или темы не настроены")
                await asyncio.sleep(300)  # Ждем 5 минут
                continue
            
            first_search = True
            
            # Определяем с какого места продолжить поиск
            start_keyword_index = 0
            start_topic_index = 0
            
            if search_progress.get('current_keyword'):
                try:
                    start_keyword_index = keywords.index(search_progress['current_keyword'])
                except ValueError:
                    start_keyword_index = 0
            
            if search_progress.get('current_topic'):
                try:
                    start_topic_index = topics.index(search_progress['current_topic'])
                except ValueError:
                    start_topic_index = 0
            
            # Перебираем все комбинации тем и ключевых слов
            for topic_idx, topic in enumerate(topics[start_topic_index:], start_topic_index):
                # Проверяем состояние is_running для каждой темы
                try:
                    import bot_interface
                    if not bot_interface.bot_data['is_running']:
                        logger.info("Остановка запрошена, прерываем цикл по темам")
                        search_active = False
                        break
                except:
                    pass
                
                if not search_active:
                    break
                
                keyword_start = start_keyword_index if topic_idx == start_topic_index else 0
                
                for keyword_idx, keyword in enumerate(keywords[keyword_start:], keyword_start):
                    # Проверяем состояние is_running для каждого ключевого слова
                    try:
                        import bot_interface
                        if not bot_interface.bot_data['is_running']:
                            logger.info("Остановка запрошена, прерываем цикл по ключевым словам")
                            search_active = False
                            break
                    except:
                        pass
                    
                    if not search_active:
                        break
                    
                    try:
                        # Сохраняем текущий прогресс
                        search_progress['current_keyword'] = keyword
                        search_progress['current_topic'] = topic
                        await save_search_progress()
                        
                        # Выполняем поиск (синхронно)
                        channels = search_channels_sync(driver, keyword, topic, first_search)
                        first_search = False
                        
                        if channels:
                            # Обрабатываем найденные каналы с обработкой FloodWait
                            try:
                                await process_found_channels(channels)
                            except FloodWaitError as e:
                                logger.warning(f"FloodWaitError при обработке каналов: {e.seconds} секунд")
                                # Ждем и продолжаем
                                await asyncio.sleep(e.seconds)
                        
                        # Увеличенная задержка между поисками для избежания FloodWait
                        await asyncio.sleep(random.uniform(3, 7))
                        
                    except FloodWaitError as e:
                        logger.warning(f"FloodWaitError в цикле поиска: {e.seconds} секунд")
                        await asyncio.sleep(e.seconds)
                        continue
                    except Exception as e:
                        logger.error(f"Ошибка поиска по '{keyword}' и '{topic}': {e}")
                        await asyncio.sleep(30)
                        continue
            
            # Сбрасываем прогресс после завершения полного цикла
            search_progress['current_keyword'] = ''
            search_progress['current_topic'] = ''
            await save_search_progress()
            
            # Ждем 30 минут перед следующим циклом поиска с проверкой состояния каждую секунду
            logger.info("Цикл поиска завершен, ожидание 30 минут...")
            for wait_second in range(1800):  # 30 минут = 1800 секунд
                try:
                    import bot_interface
                    if not bot_interface.bot_data['is_running']:
                        logger.info("Остановка запрошена во время ожидания, завершаем поиск")
                        search_active = False
                        break
                except:
                    pass
                
                if not search_active:
                    break
                await asyncio.sleep(1)
            
        except FloodWaitError as e:
            logger.warning(f"FloodWaitError в основном цикле: {e.seconds} секунд")
            await asyncio.sleep(e.seconds)
            continue
        except Exception as e:
            logger.error(f"Критическая ошибка в цикле поиска: {e}")
            if driver:
                try:
                    driver.quit()
                except:
                    pass
                driver = None
            await asyncio.sleep(60)
    
    # Закрываем драйвер при завершении
    if driver:
        try:
            driver.quit()
            logger.info("Драйвер закрыт")
        except:
            pass
        driver = None
    
    logger.info("Цикл поиска каналов завершен")

async def start_search(settings: dict, shared_client: 'TelegramClient' = None):
    """Запуск поиска каналов с использованием единого клиента"""
    global search_active, current_settings, api_call_interval, shared_telethon_client
    
    # Проверяем реальное состояние поиска
    if is_search_really_active():
        logger.warning("Поиск уже запущен")
        return
    
    # Принудительно сбрасываем состояние если нужно
    if search_active:
        logger.info("Сбрасываем заблокированное состояние поиска")
        await reset_search_state()
    
    logger.info("Запуск поиска каналов...")
    current_settings = settings.copy()
    
    # Сбрасываем интервал API вызовов
    api_call_interval = 1.0
    
    # Используем переданный единый клиент
    if shared_client:
        shared_telethon_client = shared_client
        logger.info("Используем единый Telethon клиент от bot_interface")
    else:
        logger.error("Не передан единый Telethon клиент")
        return
    
    # ИСПРАВЛЕНИЕ: устанавливаем флаг ПОСЛЕ успешной настройки
    search_active = True
    
    # Запускаем поиск как фоновую задачу в том же event loop
    try:
        search_task = asyncio.create_task(start_search_in_main_loop())
        logger.info("Поиск каналов запущен как фоновая задача в основном event loop")
    except Exception as e:
        logger.error(f"Не удалось запустить поиск: {e}")
        search_active = False

async def stop_search():
    """Остановка поиска каналов"""
    global search_active, driver, shared_telethon_client
    
    logger.info("Остановка поиска каналов...")
    search_active = False
    
    # Сохраняем прогресс перед остановкой
    await save_search_progress()
    
    if driver:
        try:
            driver.quit()
            logger.info("Драйвер закрыт")
        except Exception as e:
            logger.error(f"Ошибка закрытия драйвера: {e}")
        driver = None
    
    # Клиент принадлежит bot_interface, не закрываем его
    shared_telethon_client = None
    
    logger.info("Поиск каналов остановлен")

def get_statistics():
    """Получение статистики поиска"""
    return {
        'found_channels': len(found_channels),
        'search_active': search_active,
        'current_progress': search_progress.copy(),
        'api_call_interval': api_call_interval,
        'flood_wait_delays': flood_wait_delays.copy()
    }

# Основная функция для тестирования
async def main():
    """Тестирование модуля"""
    test_settings = {
        'keywords': ['тест', 'пример'],
        'topics': ['Технологии', 'Образование']
    }
    
    await start_search(test_settings)
    
    # Тест в течение 2 минут
    await asyncio.sleep(120)
    
    await stop_search()

if __name__ == "__main__":
    asyncio.run(main())

async def start_search_in_main_loop():
    """Запуск поиска в основном event loop (исправление проблемы event loop)"""
    global search_active
    
    try:
        logger.info("Запуск поиска каналов в основном event loop")
        
        # Создаем задачу поиска в текущем event loop
        search_task = asyncio.create_task(search_loop_async())
        
        # Запускаем задачу
        await search_task
        
    except Exception as e:
        logger.error(f"Ошибка запуска поиска в основном loop: {e}")
        search_active = False
    finally:
        search_active = False
        logger.info("Поиск в основном loop завершен")
        logger.info("Поиск в основном loop завершен")

def start_search_background():
    """Запуск поиска как фоновой задачи в основном event loop (упрощённая версия)"""
    try:
        # Получаем текущий event loop
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Если loop уже запущен, создаем задачу
            search_task = loop.create_task(start_search_in_main_loop())
            logger.info("Поиск запущен как фоновая задача в основном loop")
            return search_task
        else:
            logger.error("Event loop не запущен")
            return None
    except Exception as e:
        logger.error(f"Ошибка запуска фонового поиска: {e}")
        return None

async def reset_search_state():
    """Принудительный сброс состояния поиска для устранения блокировок"""
    global search_active, driver, shared_telethon_client
    
    logger.info("Принудительный сброс состояния поиска...")
    search_active = False
    
    if driver:
        try:
            driver.quit()
        except Exception as e:
            logger.debug(f"Ошибка закрытия драйвера при сбросе: {e}")
        driver = None
    
    logger.info("Состояние поиска сброшено")

def is_search_really_active():
    """Проверка реального состояния поиска"""
    global search_active, driver, shared_telethon_client
    
    # Если флаг установлен, но нет драйвера и клиента - поиск не активен
    if search_active and not driver and not shared_telethon_client:
        logger.warning("Обнаружено неконсистентное состояние поиска - сбрасываем флаг")
        search_active = False
        return False
    
    return search_active