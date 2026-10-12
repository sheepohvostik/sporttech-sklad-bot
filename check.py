import gspread

gc = gspread.service_account(filename="credentials.json")
sh = gc.open_by_key("1kpg6gWaSBFJLw7NIX4vlvyoJZmFnDb80OIPTbI-s1JY")
print("Сотрудники:", sh.worksheet("Сотрудники").row_values(1))
print("Журнал:", sh.worksheet("Журнал").row_values(1))
print("Справочники:", sh.worksheet("Справочники").row_values(4))
