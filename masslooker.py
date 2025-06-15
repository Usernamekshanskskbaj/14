import asyncio
import logging
import random
import time
from datetime import datetime
from typing import List, Optional, Set, Callable, Any, Dict
import os

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(f'masslooker_log_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

try:
    from telethon import TelegramClient, events
    from telethon.errors import ChannelPrivateError, ChatWriteForbiddenError, FloodWaitError, UserNotParticipantError
    from telethon.tl.types import Channel, Chat, MessageMediaPhoto, MessageMediaDocument
    from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest, GetFullChannelRequest
    from telethon.tl.functions.messages import SendReactionRequest, GetAvailableReactionsRequest, GetDiscussionMessageRequest
    from telethon.tl.types import ReactionEmoji, ReactionCustomEmoji
    import g4f
except ImportError as e:
    logger.error(f"Ошибка импорта библиотек: {e}")
    raise

# Глобальные переменные
masslooking_active = False
shared_client: Optional[TelegramClient] = None
settings = {}
processed_channels: Set[str] = set()
masslooking_progress = {'current_channel': '', 'processed_count': 0}
statistics = {
    'comments_sent': 0,
    'reactions_set': 0,
    'channels_processed': 0,
    'errors': 0,
    'flood_waits': 0,
    'total_flood_wait_time': 0
}

# Круговая обработка каналов
channel_processing_queue = {}
current_channel_iterator = None
channels_in_rotation = []

# Глобальная переменная для промпта
COMMENT_PROMPT = None

# Настройки FloodWait
FLOOD_WAIT_SETTINGS = {
    'max_retries': 5,
    'max_wait_time': 7200,  # максимальное время ожидания (2 часа)
    'enable_exponential_backoff': True,
    'check_interval': 10,  # интервал проверки состояния во время ожидания
    'backoff_multiplier': 1.5  # множитель для экспоненциального backoff
}

# Положительные реакции для Telegram
DEFAULT_POSITIVE_REACTIONS = [
    '👍', '❤️', '🔥', '🥰', '👏', '😍', '🤩', '💯', '⭐',
    '🎉', '🙏', '💪', '👌', '✨', '🌟', '🚀'
]

def check_bot_running() -> bool:
    """Проверка состояния работы бота"""
    try:
        import bot_interface
        return bot_interface.bot_data.get('is_running', True)
    except:
        return masslooking_active

async def smart_wait(wait_time: int, operation_name: str = "operation") -> bool:
    """Умное ожидание с возможностью прерывания и экспоненциальным backoff"""
    original_wait_time = wait_time
    
    # Ограничиваем максимальное время ожидания
    if wait_time > FLOOD_WAIT_SETTINGS['max_wait_time']:
        wait_time = FLOOD_WAIT_SETTINGS['max_wait_time']
        logger.warning(f"FloodWait для {operation_name}: {original_wait_time}с ограничен до {wait_time}с")
    
    logger.info(f"Ожидание FloodWait для {operation_name}: {wait_time} секунд")
    
    # Обновляем статистику
    statistics['flood_waits'] += 1
    statistics['total_flood_wait_time'] += wait_time
    
    # Разбиваем ожидание на части для регулярной проверки состояния
    check_interval = FLOOD_WAIT_SETTINGS['check_interval']
    chunks = wait_time // check_interval
    remainder = wait_time % check_interval
    
    # Ожидаем по частям с проверкой состояния
    for i in range(chunks):
        if not check_bot_running():
            logger.info(f"Остановка запрошена во время FloodWait для {operation_name}")
            return False
        
        progress = (i + 1) * check_interval
        remaining = wait_time - progress
        logger.debug(f"FloodWait {operation_name}: прошло {progress}с, осталось {remaining}с")
        
        await asyncio.sleep(check_interval)
    
    # Ожидаем оставшееся время
    if remainder > 0:
        if not check_bot_running():
            return False
        await asyncio.sleep(remainder)
    
    logger.info(f"FloodWait для {operation_name} завершен")
    return True

async def handle_flood_wait(func: Callable, *args, operation_name: str = None, max_retries: int = None, **kwargs) -> Any:
    """Универсальная обработка FloodWait для любых функций"""
    if operation_name is None:
        operation_name = func.__name__ if hasattr(func, '__name__') else "operation"
    
    if max_retries is None:
        max_retries = FLOOD_WAIT_SETTINGS['max_retries']
    
    base_delay = 1  # базовая задержка между попытками
    
    for attempt in range(max_retries):
        try:
            # Проверяем состояние перед каждой попыткой
            if not check_bot_running():
                logger.info(f"Остановка запрошена перед выполнением {operation_name}")
                return None
            
            logger.debug(f"Попытка {attempt + 1}/{max_retries} для {operation_name}")
            return await func(*args, **kwargs)
            
        except FloodWaitError as e:
            wait_time = e.seconds
            logger.warning(f"FloodWait при {operation_name} (попытка {attempt + 1}): {wait_time} секунд")
            
            if attempt < max_retries - 1:
                # Ожидаем FloodWait
                if not await smart_wait(wait_time, operation_name):
                    logger.info(f"Прерываем {operation_name} из-за остановки бота")
                    return None
                
                # Добавляем экспоненциальную задержку после FloodWait
                if FLOOD_WAIT_SETTINGS['enable_exponential_backoff']:
                    extra_delay = base_delay * (FLOOD_WAIT_SETTINGS['backoff_multiplier'] ** attempt)
                    logger.debug(f"Дополнительная задержка после FloodWait: {extra_delay:.1f}с")
                    await asyncio.sleep(extra_delay)
                
                continue
            else:
                logger.error(f"Превышено количество попыток для {operation_name} после FloodWait")
                statistics['errors'] += 1
                return None
                
        except Exception as e:
            logger.error(f"Ошибка при выполнении {operation_name} (попытка {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                # Небольшая задержка перед повтором при обычных ошибках
                await asyncio.sleep(random.uniform(1, 3))
                continue
            else:
                logger.error(f"Превышено количество попыток для {operation_name}")
                statistics['errors'] += 1
                return None
    
    return None

def load_comment_prompt():
    """Загрузка промпта для генерации комментариев из файла"""
    global COMMENT_PROMPT
    prompt_file = 'prompt_for_generating_comments.txt'
    
    try:
        if not os.path.exists(prompt_file):
            error_msg = f"Критическая ошибка: файл {prompt_file} не найден!"
            logger.error(error_msg)
            raise FileNotFoundError(error_msg)
        
        with open(prompt_file, 'r', encoding='utf-8') as f:
            COMMENT_PROMPT = f.read().strip()
        
        if not COMMENT_PROMPT:
            error_msg = f"Критическая ошибка: файл {prompt_file} пуст!"
            logger.error(error_msg)
            raise ValueError(error_msg)
        
        logger.info(f"Промпт успешно загружен из файла {prompt_file}")
        return COMMENT_PROMPT
        
    except Exception as e:
        error_msg = f"Критическая ошибка загрузки промпта: {e}"
        logger.error(error_msg)
        raise Exception(error_msg)

async def generate_comment(post_text: str, topics: List[str]) -> str:
    """Генерация комментария с помощью GPT-4"""
    try:
        # Проверяем, что промпт загружен
        if COMMENT_PROMPT is None:
            raise Exception("Промпт не загружен! Необходимо сначала вызвать load_comment_prompt()")
        
        # Подготавливаем промпт
        topics_text = ', '.join(topics) if topics else 'общая тематика'
        prompt = COMMENT_PROMPT
        
        # Заменяем плейсхолдеры в промпте
        if '{text_of_the_post}' in prompt:
            prompt = prompt.replace('{text_of_the_post}', post_text[:1000])
        else:
            prompt = prompt + f"\n\nТекст поста: {post_text[:1000]}"
        
        if '{topics}' in prompt:
            prompt = prompt.replace('{topics}', topics_text)
        
        # Генерируем комментарий
        response = g4f.ChatCompletion.create(
            model=g4f.models.gpt_4,
            messages=[{"role": "user", "content": prompt}],
            stream=False
        )
        
        # Очищаем ответ - убираем любые лишние символы
        comment = response.strip()
        
        # Удаляем кавычки в начале и конце если есть
        if comment.startswith('"') and comment.endswith('"'):
            comment = comment[1:-1]
        
        if comment.startswith("'") and comment.endswith("'"):
            comment = comment[1:-1]
        
        logger.info(f"Сгенерирован комментарий: {comment[:50]}...")
        return comment
        
    except Exception as e:
        logger.error(f"Ошибка генерации комментария: {e}")
        # Возвращаем простой комментарий в случае ошибки
        fallback_comments = [
            "Интересно, спасибо за пост!",
            "Полезная информация",
            "Актуальная тема",
            "Хороший материал",
            "Согласен с автором"
        ]
        return random.choice(fallback_comments)

async def get_entity_safe(identifier):
    """Безопасное получение сущности с обработкой FloodWait"""
    async def _get_entity():
        return await shared_client.get_entity(identifier)
    
    return await handle_flood_wait(_get_entity, operation_name=f"get_entity({identifier})")

async def get_full_channel_safe(entity):
    """Безопасное получение полной информации о канале с обработкой FloodWait"""
    async def _get_full_channel():
        return await shared_client(GetFullChannelRequest(entity))
    
    return await handle_flood_wait(_get_full_channel, operation_name=f"get_full_channel({entity.id})")

async def join_channel_safe(entity):
    """Безопасная подписка на канал с обработкой FloodWait"""
    async def _join_channel():
        return await shared_client(JoinChannelRequest(entity))
    
    return await handle_flood_wait(_join_channel, operation_name=f"join_channel({entity.username or entity.id})")

async def leave_channel_safe(entity):
    """Безопасная отписка от канала с обработкой FloodWait"""
    async def _leave_channel():
        return await shared_client(LeaveChannelRequest(entity))
    
    return await handle_flood_wait(_leave_channel, operation_name=f"leave_channel({entity.username or entity.id})")

async def send_message_safe(peer, message, **kwargs):
    """Безопасная отправка сообщения с обработкой FloodWait"""
    async def _send_message():
        return await shared_client.send_message(peer, message, **kwargs)
    
    peer_name = getattr(peer, 'username', None) or getattr(peer, 'id', 'unknown')
    return await handle_flood_wait(_send_message, operation_name=f"send_message_to({peer_name})")

async def send_reaction_safe(peer, msg_id, reaction):
    """Безопасная отправка реакции с обработкой FloodWait"""
    async def _send_reaction():
        return await shared_client(SendReactionRequest(
            peer=peer,
            msg_id=msg_id,
            reaction=[ReactionEmoji(emoticon=reaction)]
        ))
    
    peer_name = getattr(peer, 'username', None) or getattr(peer, 'id', 'unknown')
    return await handle_flood_wait(_send_reaction, operation_name=f"send_reaction_to({peer_name}, {msg_id})")

async def get_discussion_message_safe(peer, msg_id):
    """Безопасное получение discussion message с обработкой FloodWait"""
    async def _get_discussion_message():
        return await shared_client(GetDiscussionMessageRequest(peer=peer, msg_id=msg_id))
    
    peer_name = getattr(peer, 'username', None) or getattr(peer, 'id', 'unknown')
    return await handle_flood_wait(_get_discussion_message, operation_name=f"get_discussion_message({peer_name}, {msg_id})")

async def iter_messages_safe(entity, limit=None):
    """Безопасная итерация по сообщениям с обработкой FloodWait"""
    messages = []
    try:
        async for message in shared_client.iter_messages(entity, limit=limit):
            messages.append(message)
            # Небольшая задержка между получением сообщений
            await asyncio.sleep(0.1)
    except FloodWaitError as e:
        logger.warning(f"FloodWait при получении сообщений: {e.seconds} секунд")
        if await smart_wait(e.seconds, "iter_messages"):
            # Повторяем попытку после ожидания
            try:
                async for message in shared_client.iter_messages(entity, limit=limit):
                    messages.append(message)
                    await asyncio.sleep(0.1)
            except Exception as retry_error:
                logger.error(f"Ошибка при повторной попытке получения сообщений: {retry_error}")
        else:
            logger.info("Прерываем получение сообщений из-за остановки бота")
    except Exception as e:
        logger.error(f"Ошибка при получении сообщений: {e}")
    
    return messages

async def get_channel_available_reactions(entity) -> List[str]:
    """Получение доступных реакций конкретно в канале"""
    try:
        # Получаем полную информацию о канале с защитой от FloodWait
        full_channel = await get_full_channel_safe(entity)
        if not full_channel:
            logger.warning("Не удалось получить информацию о канале")
            return DEFAULT_POSITIVE_REACTIONS
        
        # Проверяем доступные реакции канала
        if hasattr(full_channel.full_chat, 'available_reactions'):
            available_reactions = full_channel.full_chat.available_reactions
            
            if available_reactions and hasattr(available_reactions, 'reactions'):
                channel_reactions = []
                for reaction in available_reactions.reactions:
                    if hasattr(reaction, 'emoticon'):
                        emoji = reaction.emoticon
                        # Добавляем только положительные эмодзи
                        if emoji in DEFAULT_POSITIVE_REACTIONS:
                            channel_reactions.append(emoji)
                
                if channel_reactions:
                    logger.info(f"Найдено {len(channel_reactions)} доступных положительных реакций в канале")
                    return channel_reactions
        
        # Если не удалось получить реакции канала, используем базовый набор
        logger.info("Используем базовый набор положительных реакций")
        return DEFAULT_POSITIVE_REACTIONS
        
    except Exception as e:
        logger.warning(f"Ошибка получения доступных реакций канала: {e}")
        return DEFAULT_POSITIVE_REACTIONS

async def add_reaction_to_post(message, channel_username):
    """Добавление реакции к посту с полной обработкой FloodWait"""
    try:
        # Проверяем состояние is_running
        if not check_bot_running():
            logger.info("Остановка запрошена, прерываем добавление реакции")
            return False
        
        # Получаем информацию о канале
        entity = await get_entity_safe(message.peer_id)
        if not entity:
            logger.error("Не удалось получить информацию о канале для реакции")
            return False
        
        # Получаем доступные реакции в канале
        available_reactions = await get_channel_available_reactions(entity)
        
        if not available_reactions:
            logger.warning("Нет доступных реакций в канале")
            return False
        
        # Выбираем случайную доступную положительную реакцию
        reaction = random.choice(available_reactions)
        
        # Отправляем реакцию с защитой от FloodWait
        result = await send_reaction_safe(message.peer_id, message.id, reaction)
        
        if result is not None:
            logger.info(f"Поставлена реакция {reaction} к посту {message.id}")
            statistics['reactions_set'] += 1
            
            # Обновляем статистику в bot_interface
            try:
                import bot_interface
                bot_interface.update_statistics(reactions=1)
                bot_interface.add_processed_channel_statistics(channel_username, reaction_added=True)
            except:
                pass
            
            return True
        else:
            logger.warning(f"Не удалось поставить реакцию на пост {message.id}")
            return False
            
    except Exception as e:
        logger.error(f"Критическая ошибка добавления реакции: {e}")
        statistics['errors'] += 1
        return False

async def check_post_comments_available(message) -> bool:
    """Проверка доступности комментариев под конкретным постом"""
    try:
        # Получаем информацию о канале с защитой от FloodWait
        entity = await get_entity_safe(message.peer_id)
        if not entity:
            return False
        
        # Получаем полную информацию о канале с защитой от FloodWait
        full_channel = await get_full_channel_safe(entity)
        if not full_channel:
            return False
        
        # Проверяем, есть ли linked_chat_id
        if hasattr(full_channel.full_chat, 'linked_chat_id') and full_channel.full_chat.linked_chat_id:
            # Проверяем, включены ли комментарии для этого поста
            if hasattr(message, 'replies') and message.replies:
                logger.info(f"Пост {message.id} поддерживает комментарии")
                return True
            else:
                logger.info(f"Пост {message.id} не поддерживает комментарии")
                return False
        else:
            logger.info(f"Канал не имеет группы обсуждений")
            return False
        
    except Exception as e:
        logger.warning(f"Ошибка проверки комментариев поста: {e}")
        return False

async def send_comment_to_post(message, comment_text: str, channel_username: str):
    """Отправка комментария в ответ на конкретный пост используя правильный алгоритм с полной защитой от FloodWait"""
    try:
        # Проверяем состояние is_running
        if not check_bot_running():
            logger.info("Остановка запрошена, прерываем отправку комментария")
            return False
        
        # Проверяем доступность комментариев под постом
        if not await check_post_comments_available(message):
            logger.info(f"Комментарии недоступны для поста {message.id}")
            return False
        
        # Шаг 1: Получаешь post_id в канале (у нас это message.id)
        post_id = message.id
        logger.info(f"Шаг 1: Post ID = {post_id}")
        
        # Шаг 2: Вызываешь GetDiscussionMessageRequest с защитой от FloodWait
        try:
            discussion_info = await get_discussion_message_safe(message.peer_id, post_id)
            
            if not discussion_info:
                logger.warning(f"Не удалось получить информацию о discussion для поста {post_id}")
                return await send_comment_fallback(message, comment_text, channel_username)
            
            logger.info(f"Шаг 2: Получена информация о discussion для поста {post_id}")
            
            # Получаем discussion_group и reply_to_msg_id из ответа
            if hasattr(discussion_info, 'messages') and discussion_info.messages:
                discussion_message = discussion_info.messages[0]
                discussion_group = discussion_message.peer_id
                reply_to_msg_id = discussion_message.id
                
                logger.info(f"Найдена группа обсуждений: {discussion_group}")
                logger.info(f"Reply to message ID: {reply_to_msg_id}")
                
                # Шаг 3: Отправляем сообщение с reply_to=reply_to_msg_id с защитой от FloodWait
                result = await send_message_safe(
                    discussion_group,
                    comment_text,  # Отправляем ТОЛЬКО текст комментария без префикса
                    reply_to=reply_to_msg_id
                )
                
                if result is not None:
                    logger.info(f"Шаг 3: Комментарий отправлен в ответ на пост {post_id}: {comment_text[:50]}...")
                    
                    # Формируем ссылки для статистики
                    entity = await get_entity_safe(message.peer_id)
                    if entity and hasattr(entity, 'username'):
                        post_link = f"https://t.me/{entity.username}/{post_id}"
                        comment_link = f"https://t.me/c/{str(discussion_group.channel_id)}/{result.id}"
                        
                        # Обновляем подробную статистику
                        try:
                            import bot_interface
                            bot_interface.add_processed_channel_statistics(
                                channel_username, 
                                comment_link=comment_link, 
                                post_link=post_link
                            )
                        except:
                            pass
                else:
                    logger.warning(f"Не удалось отправить комментарий для поста {post_id}")
                    return False
                
            else:
                logger.warning(f"Не удалось получить информацию о discussion message для поста {post_id}")
                return await send_comment_fallback(message, comment_text, channel_username)
                
        except Exception as discussion_error:
            logger.error(f"Ошибка при работе с GetDiscussionMessageRequest: {discussion_error}")
            # Fallback к старой логике
            return await send_comment_fallback(message, comment_text, channel_username)
        
        statistics['comments_sent'] += 1
        
        # Обновляем статистику в bot_interface
        try:
            import bot_interface
            bot_interface.update_statistics(comments=1)
        except:
            pass
        
        return True
        
    except Exception as e:
        error_text = str(e)
        # Проверяем на ошибку "You join the discussion group before commenting"
        if "You join the discussion group before commenting" in error_text:
            logger.info("Обнаружена ошибка требования вступления в группу, пытаемся вступить")
            try:
                # Получаем информацию о канале для доступа к группе обсуждений
                entity = await get_entity_safe(message.peer_id)
                if not entity:
                    return False
                
                full_channel = await get_full_channel_safe(entity)
                if not full_channel:
                    return False
                
                if hasattr(full_channel.full_chat, 'linked_chat_id') and full_channel.full_chat.linked_chat_id:
                    discussion_group = await get_entity_safe(full_channel.full_chat.linked_chat_id)
                    if discussion_group:
                        # Вступаем в группу обсуждений с защитой от FloodWait
                        join_result = await join_channel_safe(discussion_group)
                        if join_result is not None:
                            logger.info(f"Вступили в группу обсуждений {discussion_group.title}")
                            await asyncio.sleep(2)
                            # Повторяем попытку отправки комментария
                            return await send_comment_to_post(message, comment_text, channel_username)
                        else:
                            logger.error("Не удалось вступить в группу обсуждений")
                            return False
                else:
                    logger.error("Не найдена группа обсуждений для вступления")
                    return False
                    
            except Exception as join_error:
                logger.error(f"Ошибка при вступлении в группу обсуждений: {join_error}")
                return False
        else:
            logger.error(f"Ошибка отправки комментария: {e}")
            statistics['errors'] += 1
            return False

async def send_comment_fallback(message, comment_text: str, channel_username: str):
    """Fallback метод отправки комментария (старая логика) с защитой от FloodWait"""
    try:
        logger.info("Используем fallback метод отправки комментария")
        
        # Получаем информацию о канале с защитой от FloodWait
        entity = await get_entity_safe(message.peer_id)
        if not entity:
            return False
        
        # Получаем полную информацию о канале с защитой от FloodWait
        full_channel = await get_full_channel_safe(entity)
        if not full_channel:
            return False
        
        if hasattr(full_channel.full_chat, 'linked_chat_id') and full_channel.full_chat.linked_chat_id:
            # Получаем группу обсуждений с защитой от FloodWait
            discussion_group = await get_entity_safe(full_channel.full_chat.linked_chat_id)
            if discussion_group:
                # Отправляем комментарий в группу с защитой от FloodWait
                result = await send_message_safe(discussion_group, comment_text)
                if result is not None:
                    logger.info(f"Fallback: Отправлен комментарий в группу обсуждений: {comment_text[:50]}...")
                    
                    # Обновляем статистику
                    try:
                        import bot_interface
                        bot_interface.add_processed_channel_statistics(channel_username, comment_link="fallback_method")
                    except:
                        pass
                    
                    return True
                else:
                    return False
        else:
            # Если нет группы обсуждений, пытаемся отправить комментарий напрямую в канал
            result = await send_message_safe(message.peer_id, comment_text, comment_to=message.id)
            if result is not None:
                logger.info(f"Fallback: Отправлен комментарий напрямую в канал: {comment_text[:50]}...")
                return True
            else:
                return False
        
    except Exception as e:
        logger.error(f"Ошибка в fallback методе: {e}")
        return False

async def save_masslooking_progress():
    """Сохранение прогресса масслукинга в базу данных"""
    try:
        from database import db
        # Сохраняем прогресс пакетно для лучшей производительности
        progress_data = [
            ('masslooking_progress', masslooking_progress),
            ('processed_channels', list(processed_channels)),
            ('channel_processing_queue', channel_processing_queue)
        ]
        
        for key, value in progress_data:
            await db.save_bot_state(key, value)
    except Exception as e:
        logger.error(f"Ошибка сохранения прогресса масслукинга: {e}")

async def load_masslooking_progress():
    """Загрузка прогресса масслукинга из базы данных"""
    global masslooking_progress, processed_channels, channel_processing_queue
    try:
        from database import db
        
        # Загружаем прогресс масслукинга
        saved_progress = await db.load_bot_state('masslooking_progress', {})
        if saved_progress:
            masslooking_progress.update(saved_progress)
        
        # Загружаем обработанные каналы
        saved_channels = await db.load_bot_state('processed_channels', [])
        if saved_channels:
            processed_channels.update(saved_channels)
        
        # Загружаем очередь обработки каналов
        saved_queue = await db.load_bot_state('channel_processing_queue', {})
        if saved_queue:
            channel_processing_queue.update(saved_queue)
        
        logger.info(f"Загружен прогресс масслукинга: {masslooking_progress}")
        logger.info(f"Загружено обработанных каналов: {len(processed_channels)}")
        logger.info(f"Загружено каналов в очереди: {len(channel_processing_queue)}")
    except Exception as e:
        logger.error(f"Ошибка загрузки прогресса масслукинга: {e}")

async def prepare_channel_for_processing(username: str):
    """Подготовка канала для обработки с исправлением проблемы event loop"""
    try:
        if not username.startswith('@'):
            username = '@' + username
        
        logger.info(f"Подготавливаем канал {username} для обработки")
        
        # ИСПРАВЛЕНИЕ: убеждаемся что используем тот же event loop что и shared_client
        if not shared_client:
            logger.error("Shared client не инициализирован")
            return False
        
        # Получаем информацию о канале
        entity = await get_entity_safe(username)
        if not entity:
            logger.error(f"Не удалось получить информацию о канале {username}")
            return False
        
        # Подписка с задержкой
        delay_range = settings.get('delay_range', (20, 1000))
        if delay_range != (0, 0):
            join_delay = random.uniform(delay_range[0], delay_range[1])
            logger.info(f"Задержка {join_delay:.1f} секунд перед подпиской на {username}")
            
            # Разбиваем задержку с проверкой состояния
            delay_chunks = int(join_delay)
            remaining_delay = join_delay - delay_chunks
            
            for _ in range(delay_chunks):
                if not check_bot_running():
                    logger.info("Остановка запрошена во время задержки подписки")
                    return False
                await asyncio.sleep(1)
            
            if remaining_delay > 0:
                await asyncio.sleep(remaining_delay)
        
        # Подписываемся на канал
        try:
            if hasattr(entity, 'left') and entity.left:
                join_result = await join_channel_safe(entity)
                if join_result is not None:
                    logger.info(f"Подписались на канал {username}")
                    
                    # Задержка после подписки
                    if delay_range != (0, 0):
                        post_join_delay = random.uniform(2, 5)
                        await asyncio.sleep(post_join_delay)
                else:
                    logger.warning(f"Не удалось подписаться на канал {username}")
            else:
                logger.info(f"Уже подписаны на канал {username}")
        except Exception as e:
            logger.warning(f"Ошибка подписки на канал {username}: {e}")
        
        # Определяем количество постов
        posts_range = settings.get('posts_range', (1, 5))
        if posts_range[0] != posts_range[1]:
            posts_count = random.randint(posts_range[0], posts_range[1])
        else:
            posts_count = posts_range[0]
        
        logger.info(f"Канал {username}: планируется обработать {posts_count} постов")
        
        # Получаем сообщения канала
        messages = await iter_messages_safe(entity, limit=posts_count * 3)
        
        # Фильтруем сообщения
        valid_messages = []
        for message in messages:
            if message.text and len(message.text.strip()) >= 10:
                valid_messages.append(message)
                if len(valid_messages) >= posts_count:
                    break
        
        if not valid_messages:
            logger.warning(f"В канале {username} не найдено подходящих постов")
            return False
        
        # Добавляем канал в очередь обработки
        channel_processing_queue[username] = {
            'posts_processed': 0,
            'total_posts': len(valid_messages),
            'messages': valid_messages,
            'entity': entity
        }
        
        logger.info(f"Канал {username} готов: {len(valid_messages)} постов")
        return True
        
    except Exception as e:
        logger.error(f"Ошибка подготовки канала {username}: {e}")
        return False

async def process_single_post_from_channel(username: str) -> bool:
    """Обработка одного поста из канала (круговая логика)"""
    try:
        if username not in channel_processing_queue:
            logger.warning(f"Канал {username} не найден в очереди обработки")
            return False
        
        channel_data = channel_processing_queue[username]
        
        # Проверяем, есть ли еще посты для обработки
        if channel_data['posts_processed'] >= channel_data['total_posts']:
            logger.info(f"Все посты в канале {username} обработаны")
            return False
        
        # Получаем текущий пост для обработки
        current_post_index = channel_data['posts_processed']
        message = channel_data['messages'][current_post_index]
        entity = channel_data['entity']
        
        logger.info(f"Обрабатываем пост {current_post_index + 1}/{channel_data['total_posts']} в канале {username}")
        
        topics = settings.get('topics', [])
        
        try:
            # Проверяем доступность комментариев под постом
            post_supports_comments = await check_post_comments_available(message)
            
            if post_supports_comments:
                # Генерируем и отправляем комментарий
                logger.info(f"Генерируем комментарий для поста {message.id}")
                comment = await generate_comment(message.text, topics)
                
                logger.info(f"Отправляем комментарий для поста {message.id}: {comment[:50]}...")
                comment_sent = await send_comment_to_post(message, comment, username)
                
                if comment_sent:
                    logger.info(f"Комментарий успешно отправлен для поста {message.id}")
                    # Добавляем задержку между комментарием и реакцией
                    delay = random.uniform(2, 8)
                    await asyncio.sleep(delay)
                else:
                    logger.warning(f"Не удалось отправить комментарий для поста {message.id}")
            else:
                logger.info(f"Пост {message.id} не поддерживает комментарии, пропускаем комментирование")
            
            # Ставим реакцию (всегда пытаемся поставить реакцию)
            logger.info(f"Ставим реакцию на пост {message.id}")
            reaction_result = await add_reaction_to_post(message, username)
            if reaction_result:
                logger.info(f"Реакция успешно поставлена на пост {message.id}")
            else:
                logger.warning(f"Не удалось поставить реакцию на пост {message.id}")
            
            # Увеличиваем счетчик обработанных постов
            channel_processing_queue[username]['posts_processed'] += 1
            
            logger.info(f"Пост {current_post_index + 1}/{channel_data['total_posts']} в канале {username} обработан")
            
            # Сохраняем прогресс
            await save_masslooking_progress()
            
            return True
            
        except Exception as e:
            logger.error(f"Ошибка обработки поста {message.id} в канале {username}: {e}")
            statistics['errors'] += 1
            # Увеличиваем счетчик даже при ошибке, чтобы не зацикливаться
            channel_processing_queue[username]['posts_processed'] += 1
            return False
        
    except Exception as e:
        logger.error(f"Критическая ошибка обработки поста в канале {username}: {e}")
        return False

async def finalize_channel_processing(username: str):
    """Завершение обработки канала: отписка и очистка"""
    try:
        if username in channel_processing_queue:
            channel_data = channel_processing_queue[username]
            entity = channel_data['entity']
            
            # Отписываемся от канала
            leave_result = await leave_channel_safe(entity)
            if leave_result is not None:
                logger.info(f"Отписались от канала {username}")
            else:
                logger.warning(f"Не удалось отписаться от канала {username}")
            
            # Удаляем канал из очереди обработки
            del channel_processing_queue[username]
            
            # Добавляем в список полностью обработанных
            processed_channels.add(username)
            
            # Обновляем статистику
            statistics['channels_processed'] += 1
            masslooking_progress['processed_count'] += 1
            
            # Обновляем статистику в bot_interface
            try:
                import bot_interface
                bot_interface.update_statistics(channels=1)
            except:
                pass
            
            # Сохраняем прогресс
            await save_masslooking_progress()
            
            logger.info(f"Обработка канала {username} полностью завершена")
            
    except Exception as e:
        logger.error(f"Ошибка завершения обработки канала {username}: {e}")

async def masslooking_worker():
    """Рабочий процесс масслукинга с круговой обработкой каналов"""
    global masslooking_active, current_channel_iterator, channels_in_rotation, settings
    
    # Загружаем прогресс при запуске
    await load_masslooking_progress()
    logger.info("Рабочий процесс масслукинга запущен (круговая обработка)")
    
    while masslooking_active:
        try:
            # Проверяем состояние is_running
            if not check_bot_running():
                logger.info("Остановка запрошена в bot_interface, завершаем масслукинг")
                masslooking_active = False
                break
            
            # Обновляем настройки в реальном времени
            try:
                import bot_interface
                current_settings = bot_interface.get_bot_settings()
                if current_settings != settings:
                    settings.update(current_settings)
                    logger.info("Настройки масслукинга обновлены в реальном времени")
            except Exception as e:
                logger.warning(f"Не удалось обновить настройки: {e}")
            
            # Обновляем список каналов в ротации
            channels_in_rotation = list(channel_processing_queue.keys())
            
            # Если нет каналов в обработке, ждем новые
            if not channels_in_rotation:
                logger.debug("Нет каналов в обработке, ожидаем...")
                await asyncio.sleep(5)
                continue
            
            # Создаем итератор для круговой обработки, если его нет
            if current_channel_iterator is None:
                current_channel_iterator = iter(channels_in_rotation)
            
            try:
                # Получаем следующий канал из ротации
                current_channel = next(current_channel_iterator)
            except StopIteration:
                # Если итератор закончился, создаем новый (начинаем сначала)
                current_channel_iterator = iter(channels_in_rotation)
                if channels_in_rotation:  # Проверяем, что список не пуст
                    current_channel = next(current_channel_iterator)
                else:
                    continue
            
            # Проверяем, что канал все еще в очереди
            if current_channel not in channel_processing_queue:
                # Канал был удален, обновляем итератор
                current_channel_iterator = None
                continue
            
            logger.info(f"Обрабатываем следующий пост из канала: {current_channel}")
            
            # Проверяем лимит каналов с учетом каналов в очереди
            max_channels = settings.get('max_channels', 150)
            total_channels = len(processed_channels) + len(channel_processing_queue)
            if max_channels != float('inf') and total_channels >= max_channels:
                logger.info(f"Достигнут лимит каналов: {max_channels} (обработано: {len(processed_channels)}, в очереди: {len(channel_processing_queue)})")
                await asyncio.sleep(60)
                continue
            
            # Обрабатываем один пост из текущего канала
            post_processed = await process_single_post_from_channel(current_channel)
            
            # Проверяем, завершена ли обработка этого канала
            if current_channel in channel_processing_queue:
                channel_data = channel_processing_queue[current_channel]
                if channel_data['posts_processed'] >= channel_data['total_posts']:
                    logger.info(f"Канал {current_channel} полностью обработан")
                    await finalize_channel_processing(current_channel)
                    # Сбрасываем итератор, так как список каналов изменился
                    current_channel_iterator = None
            
            # Задержка между действиями
            delay_range = settings.get('delay_range', (20, 1000))
            if delay_range != (0, 0):
                action_delay = random.uniform(delay_range[0], delay_range[1])
                logger.info(f"Задержка {action_delay:.1f} секунд перед следующим действием")
                
                # Разбиваем задержку на части с проверкой состояния
                delay_chunks = int(action_delay)
                remaining_delay = action_delay - delay_chunks
                
                for _ in range(delay_chunks):
                    if not check_bot_running():
                        logger.info("Остановка запрошена во время задержки")
                        masslooking_active = False
                        break
                    await asyncio.sleep(1)
                
                if remaining_delay > 0 and masslooking_active:
                    await asyncio.sleep(remaining_delay)
            
        except Exception as e:
            logger.error(f"Ошибка в рабочем процессе масслукинга: {e}")
            await asyncio.sleep(30)
    
    logger.info("Рабочий процесс масслукинга завершен")

async def add_channel_to_queue(username: str):
    """Добавление канала в очередь обработки"""
    # Проверяем лимит каналов перед добавлением
    max_channels = settings.get('max_channels', 150)
    total_channels = len(processed_channels) + len(channel_processing_queue)
    
    if max_channels != float('inf') and total_channels >= max_channels:
        logger.info(f"Достигнут лимит каналов ({max_channels}), канал {username} не добавлен в очередь")
        return
    
    if username not in processed_channels and username not in channel_processing_queue:
        # Подготавливаем канал для обработки
        success = await prepare_channel_for_processing(username)
        if success:
            logger.info(f"Канал {username} добавлен в очередь круговой обработки")
            
            # Обновляем статистику очереди в bot_interface
            try:
                import bot_interface
                queue_list = list(channel_processing_queue.keys())
                bot_interface.update_queue_statistics(queue_list)
            except:
                pass
        else:
            logger.warning(f"Не удалось подготовить канал {username} для обработки")
    else:
        logger.info(f"Канал {username} уже в обработке или обработан, пропускаем")

async def start_masslooking(telegram_client: TelegramClient, masslooking_settings: dict):
    """Запуск масслукинга с единым клиентом"""
    global masslooking_active, shared_client, settings
    
    if masslooking_active:
        logger.warning("Масслукинг уже запущен")
        return
    
    logger.info("Запуск масслукинга с круговой обработкой каналов...")
    
    # Загружаем промпт для генерации комментариев
    try:
        load_comment_prompt()
    except Exception as e:
        error_msg = f"КРИТИЧЕСКАЯ ОШИБКА: Не удалось загрузить промпт! {e}"
        logger.error(error_msg)
        raise Exception(error_msg)
    
    # Используем переданный единый клиент
    shared_client = telegram_client
    settings = masslooking_settings.copy()
    masslooking_active = True
    
    logger.info(f"Настройки масслукинга: {settings}")
    logger.info(f"Настройки FloodWait: {FLOOD_WAIT_SETTINGS}")
    
    # Запускаем рабочий процесс
    asyncio.create_task(masslooking_worker())
    
    logger.info("Масслукинг запущен с круговой обработкой каналов и полной защитой от FloodWait")

async def stop_masslooking():
    """Остановка масслукинга"""
    global masslooking_active, current_channel_iterator, channel_processing_queue
    
    logger.info("Остановка масслукинга...")
    masslooking_active = False
    current_channel_iterator = None
    
    # Сохраняем прогресс перед остановкой
    await save_masslooking_progress()
    
    logger.info(f"Масслукинг остановлен, в очереди осталось {len(channel_processing_queue)} каналов")

def get_statistics():
    """Получение статистики масслукинга включая FloodWait"""
    avg_flood_wait = 0
    if statistics['flood_waits'] > 0:
        avg_flood_wait = statistics['total_flood_wait_time'] / statistics['flood_waits']
    
    return {
        **statistics,
        'progress': masslooking_progress.copy(),
        'queue_size': len(channel_processing_queue),
        'channels_in_rotation': len(channel_processing_queue),
        'average_flood_wait_time': round(avg_flood_wait, 2),
        'flood_wait_settings': FLOOD_WAIT_SETTINGS.copy()
    }

def reset_statistics():
    """Сброс статистики"""
    global statistics, masslooking_progress
    statistics = {
        'comments_sent': 0,
        'reactions_set': 0,
        'channels_processed': 0,
        'errors': 0,
        'flood_waits': 0,
        'total_flood_wait_time': 0
    }
    masslooking_progress = {'current_channel': '', 'processed_count': 0}

def update_flood_wait_settings(new_settings: dict):
    """Обновление настроек FloodWait"""
    global FLOOD_WAIT_SETTINGS
    FLOOD_WAIT_SETTINGS.update(new_settings)
    logger.info(f"Настройки FloodWait обновлены: {FLOOD_WAIT_SETTINGS}")

# Основная функция для тестирования
async def main():
    """Тестирование модуля"""
    # Этот код предназначен только для тестирования
    print("Модуль masslooker готов к работе с круговой обработкой каналов и полной защитой от FloodWait")
    print("Для запуска используйте функции start_masslooking() и add_channel_to_queue()")
    print(f"Настройки FloodWait: {FLOOD_WAIT_SETTINGS}")

if __name__ == "__main__":
    asyncio.run(main())