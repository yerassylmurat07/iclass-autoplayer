#!/bin/bash

# Активируем виртуальное окружение
source .venv/bin/activate

# Находим путь до библиотеки customtkinter
CTK_PATH=$(python -c "import customtkinter, os; print(os.path.dirname(customtkinter.__file__))")

echo "Начинаю сборку приложения для macOS..."
echo "Путь к CustomTkinter: $CTK_PATH"

# Собираем приложение
pyinstaller --noconfirm --onedir --windowed --name "iClassAutoPlayer" --add-data "$CTK_PATH:customtkinter" gui_app.py

echo "✅ Сборка завершена!"
echo "Твое приложение находится в папке dist/iClassAutoPlayer.app"
