import logging

import gspread
from oauth2client.service_account import ServiceAccountCredentials

creds_file_path = '/root/mail_spy/google_creds.json'

scope = ['https://www.googleapis.com/auth/spreadsheets', "https://www.googleapis.com/auth/drive"]
credentials = ServiceAccountCredentials.from_json_keyfile_name(creds_file_path, scope)


def add_new_site_request(request):
    logging.info('Добавляем новую запись в Google таблицу')
    client = gspread.authorize(credentials)
    spreadsheet = client.open("Заявки на домашние группы")
    worksheet = spreadsheet.worksheet('Заявки с сайта')
    values = worksheet.get_all_values()
    cell_values = request.to_list()
    first_empty_row = len(values) + 1
    for index, value in enumerate(cell_values):
        worksheet.update_cell(first_empty_row, index + 1, value)
    logging.info('Добавлено новое значение в таблицу заявок в ДГ')
