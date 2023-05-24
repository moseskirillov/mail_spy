import base64
import logging
import os
import re
import time
from datetime import datetime, timedelta

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from database_connect import connect_to_bot
from models import SiteRequest, HTMLTagStripper
from sheets import add_new_site_request

ADMIN = os.getenv('ADMIN_ID')
current_dir = os.getcwd()

creds_file_path = os.path.join(current_dir, 'google_creds.json')
token_file_path = os.path.join(current_dir, 'token.json')
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']


def get_and_parse_mails():
    creds = google_creds_check()
    try:
        service = build('gmail', 'v1', credentials=creds)
        today = datetime.now().date()
        yesterday = today - timedelta(days=1)
        tomorrow = today + timedelta(days=1)
        formatted_date_yesterday = yesterday.strftime("%Y/%m/%d").replace('/', '/')
        tomorrow_date_today = tomorrow.strftime("%Y/%m/%d").replace('/', '/')
        results = service.users().messages().list(userId='me',
                                                  q=f'after:{formatted_date_yesterday} before:{tomorrow_date_today}').execute()
        messages = results.get('messages')

        if not messages:
            return

        for message in messages:
            payload = service.users().messages().get(userId='me', id=message['id']).execute()['payload']
            headers = payload['headers']
            date = ''
            subject = ''

            for header in headers:
                name = header['name']
                value = header['value']
                if name.lower() == 'subject':
                    subject = value
                elif name.lower() == 'date':
                    date = value

            if 'body' in payload and (subject == 'Домашняя группа' or subject == 'Регистрация на Домашнюю группу'):
                site_request = create_site_request(payload, date)
                check_and_send_new_request(site_request)

    except HttpError as error:
        error_text = f'Произошла ошибка парсинга писем по заявкам ДГ с почты: {error}'
        logging.error(error_text)
        send_message(ADMIN, error_text)


def check_and_send_new_request(site_request):
    with connect_to_bot, connect_to_bot.cursor() as cursor:
        cursor.execute(f"""SELECT count(*) 
                          FROM site_requests 
                          WHERE name = \'{site_request.name}\'
                          AND last_name = \'{site_request.surname}\'
                          AND phone = \'{site_request.phone}\';
                       """)
        result = cursor.fetchone()
        logging.info(f'Проверили количество совпадающих записей в базе, получили {result[0]}')

        if result[0] == 0:
            logging.info('Вставляем запись в базу')
            cursor.execute(f"""INSERT INTO site_requests (date, name, last_name, age, city, email, phone, leader) 
                               VALUES (\'{site_request.date}\', \'{site_request.name}\', \'{site_request.surname}\',
                               \'{site_request.age}\', \'{site_request.city}\', \'{site_request.email}\', 
                               \'{site_request.phone}\', \'{site_request.group}\');
                            """)

            logging.info('Добавляем новую запись в Google таблицу')
            add_new_site_request(site_request)
            logging.info('Отправляем сообщения лидерам в Telegram')
            send_site_request_to_leader(site_request, cursor)


def google_creds_check():
    creds = None
    if os.path.exists(token_file_path):
        creds = Credentials.from_authorized_user_file(token_file_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_file_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_file_path, 'w') as token:
            token.write(creds.to_json())
    return creds


def create_site_request(payload, date):
    date_obj = datetime.strptime(date, "%a, %d %b %Y %H:%M:%S %z (%Z)")
    formatted_date = date_obj.strftime("%d.%m.%Y")
    tag_stripper = HTMLTagStripper()
    body = base64.urlsafe_b64decode(payload['body']['data']).decode()
    tag_stripper.feed(body)
    stripped_text = ' '.join(tag_stripper.stripped_text)

    name = extract_field_from_text(r'Имя\s+(\S+)', stripped_text)
    surname = extract_field_from_text(r'Фамилия\s+(\S+)', stripped_text)
    age = extract_field_from_text(r'Полных\s+лет\s+\(Возраст\)\s+(\S+)', stripped_text)
    city = extract_field_from_text(r'Город\s+(\S+)', stripped_text)
    email = extract_field_from_text(r'E-mail\s+(\S+)', stripped_text)
    phone = extract_field_from_text(r'Телефон\s+(\S+)', stripped_text)
    group_parts = extract_field_from_text(r'ВЫБРАННАЯ\s+ДОМАШНЯЯ\s+ГРУППА\s+(\S+\s+\S+)', stripped_text)
    first_name, last_name = group_parts.split()
    group = f'{last_name} {first_name}'

    return SiteRequest(formatted_date, name, surname, age, city, email, phone, group)


def extract_field_from_text(pattern, text):
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def send_site_request_to_leader(from_site_request, cursor):
    cursor.execute(f"""SELECT gl.name, gl.telegram_id, rl.telegram_id
                       FROM regionals_groups
                       LEFT JOIN group_leaders gl ON gl.id = regionals_groups.group_leader_id
                       LEFT JOIN regional_leaders rl ON rl.id = regionals_groups.regional_leader_id
                       WHERE gl.name = \'{from_site_request.group}\';
                    """)

    result = cursor.fetchone()

    message_to_group_leader = f'С сайта пришла заявка на присоединение ' \
                              f'к Вашей домашней группе.\nКонтакт человека:\n\n' \
                              f'Имя: <b>{from_site_request.name}</b>\n' \
                              f'Фамилия: <b>{from_site_request.surname}</b>\n' \
                              f'Телефон: <b>{from_site_request.phone}</b>'

    if result:
        leader_name, leader_telegram, regional_leader_telegram = result

        message_to_regional_leader = f'С сайта пришла заявка на присоединение ' \
                                     f'к домашней группе лидера по имени \n<b>{leader_name}</b>.\n' \
                                     f'Контакт человека:\n\n' \
                                     f'Имя: <b>{from_site_request.name}</b>\n' \
                                     f'Фамилия: <b>{from_site_request.surname}</b>\n' \
                                     f'Телефон: <b>{from_site_request.phone}</b>'

        time.sleep(5)
        response_text_to_group_leader = send_message(
            leader_telegram,
            message_to_group_leader
        )

        check_response(response_text_to_group_leader)

        time.sleep(5)
        response_text_to_regional_leader = send_message(regional_leader_telegram, message_to_regional_leader)

        check_response(response_text_to_regional_leader)

    else:
        logging.error(f'Ошибка получения данных по ДГ для лидера {from_site_request.group}')
        send_message(ADMIN, message_to_group_leader)
        send_contact(
            ADMIN,
            from_site_request.phone,
            from_site_request.name,
            from_site_request.surname
        )


def send_message(chat_id, text):
    bot_token = os.getenv("BOT_TOKEN")
    response = requests.get(
        f'https://api.telegram.org/bot{bot_token}/sendMessage',
        params={
            'chat_id': ADMIN,
            'text': text,
            'parse_mode': 'HTML'
        }
    )
    return response


def send_contact(chat_id, phone_number, first_name, last_name):
    bot_token = os.getenv("BOT_TOKEN")
    response = requests.post(
        f'https://api.telegram.org/bot{bot_token}/sendContact',
        json={
            'chat_id': ADMIN,
            'phone_number': phone_number,
            'first_name': first_name,
            'last_name': last_name
        }
    )
    return response


def check_response(response):
    if response.status_code == 200:
        logging.info('Сообщение успешно отправлено.')
    else:
        logging.error('Произошла ошибка при отправке сообщения.')
        logging.error(response.json())


if __name__ == '__main__':
    get_and_parse_mails()