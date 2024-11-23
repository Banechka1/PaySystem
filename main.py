from flask import Flask, request, jsonify, render_template, redirect, url_for, session, flash
import sqlite3
import redis
import os
import hashlib
import random
import time
import logging
from kafka import KafkaProducer
import json

app = Flask(__name__)
app.secret_key = 'your_secret_key'  # Установите секретный ключ для сессий

CURRENCY_RATES = {
    'RUB': 1,
    "USD": 100.0,
    "EUR": 105.0,
    "BTC": 10135623.0
}

# Настройка баз данных
def init_db():
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        username TEXT UNIQUE NOT NULL,
                        password TEXT NOT NULL,
                        external_balance_rub REAL DEFAULT 0,
                        external_balance_usd REAL DEFAULT 0,
                        external_balance_eur REAL DEFAULT 0,
                        external_balance_btc REAL DEFAULT 0,
                        internal_balance REAL DEFAULT 0)''')
    conn.commit()
    conn.close()

    conn = sqlite3.connect('transactions.db')
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS transactions (
                        id TEXT PRIMARY KEY,
                        user_id INTEGER,
                        amount REAL,
                        currency TEXT DEFAULT 'RUB',
                        status TEXT,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

# Настройка Kafka
producer = KafkaProducer(
    bootstrap_servers=['localhost:9092'],  # Убедитесь, что используете правильный адрес и порт
    value_serializer=lambda v: json.dumps(v).encode('utf-8')
)
producer.send('transactions', value={'test': 'message'})
producer.flush()

# Настройка Redis
redis_client = redis.StrictRedis(host='localhost', port=6379, db=0)

@app.route('/')
def home():
    if 'user_id' not in session:
        return redirect(url_for('login'))  # Перенаправляем на страницу логина, если не авторизован

    user_id = session['user_id']
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    
    # Получаем данные пользователя с балансами в разных валютах
    cursor.execute('SELECT username, external_balance_rub, external_balance_usd, external_balance_eur, external_balance_btc, internal_balance FROM users WHERE id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()

    # Если пользователь не найден, очищаем сессию
    if not result:
        session.clear()
        return redirect(url_for('login'))

    # Формируем структуру данных
    user = {
        'username': result[0],
        'external_balances': {
            'RUB': result[1],
            'USD': result[2],
            'EUR': result[3],
            'BTC': result[4],
        },
        'internal_balance': result[5],
    }

    # Подключаемся к базе данных транзакций
    conn_trans = sqlite3.connect('transactions.db')
    cursor_trans = conn_trans.cursor()
    cursor_trans.execute('SELECT amount, status, timestamp FROM transactions WHERE user_id = ? ORDER BY timestamp DESC LIMIT 10', (user_id,))
    transactions = cursor_trans.fetchall()
    conn_trans.close()

    return render_template('index.html', user=user, transactions=transactions, currency_rates=CURRENCY_RATES)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()
        try:
            cursor.execute('INSERT INTO users (username, password, external_balance_rub, external_balance_usd, external_balance_eur, external_balance_btc) VALUES (?, ?, ?, ?, ?, ?)', (username, password, 10000, 500, 400, 100))
            conn.commit()
        except sqlite3.IntegrityError:
            return 'Пользователь с таким именем уже существует', 400
        finally:
            conn.close()

        return redirect(url_for('login'))

    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()
        cursor.execute('SELECT id FROM users WHERE username = ? AND password = ?', (username, password))
        user = cursor.fetchone()
        conn.close()

        if user:
            session['user_id'] = user[0]
            return redirect(url_for('home'))
        else:
            flash('Неверные имя пользователя или пароль', 'error')  # Добавляем сообщение об ошибке через flash
            return redirect(url_for('login'))

    return render_template('login.html')

@app.route('/transfer', methods=['POST'])
def transfer():
    data = request.json
    if not data or 'user_id' not in data or 'amount' not in data or 'currency' not in data:
        return jsonify({'error': 'Отсутствуют обязательные параметры'}), 400

    user_id = data['user_id']
    transfer_amount = data['amount']
    currency = data['currency']

    # Проверка поддержки валют
    if currency not in CURRENCY_RATES:
        return jsonify({'error': 'Неподдерживаемая валюта'}), 400
    rate = CURRENCY_RATES[currency]

    # Конвертация в рубли
    amount_in_rub = transfer_amount * rate

    # Минимальная сумма перевода
    if amount_in_rub < 100:
        return jsonify({'error': 'Минимальная сумма платежа 100 руб.'}), 400

    try:
        with sqlite3.connect('users.db') as conn_users, sqlite3.connect('transactions.db') as conn_trans:
            cursor_users = conn_users.cursor()

            # Получаем балансы пользователя
            cursor_users.execute('''
                SELECT external_balance_rub, external_balance_usd, external_balance_eur, external_balance_btc, internal_balance 
                FROM users WHERE id = ?
            ''', (user_id,))
            user = cursor_users.fetchone()

            if user is None:
                return jsonify({'error': 'Пользователь не найден'}), 404

            external_balance_rub, external_balance_usd, external_balance_eur, external_balance_btc, internal_balance = user

            # Определяем валютный счет
            if currency == 'RUB':
                current_balance = external_balance_rub
                balance_field = 'external_balance_rub'
            elif currency == 'USD':
                current_balance = external_balance_usd
                balance_field = 'external_balance_usd'
            elif currency == 'EUR':
                current_balance = external_balance_eur
                balance_field = 'external_balance_eur'
            elif currency == 'BTC':
                current_balance = external_balance_btc
                balance_field = 'external_balance_btc'
            else:
                return jsonify({'error': 'Неподдерживаемая валюта'}), 400

            # Проверяем, достаточно ли средств
            if transfer_amount > current_balance:
                return jsonify({'error': 'Недостаточно средств на счете'}), 400

            # Обновляем балансы
            new_external_balance = current_balance - transfer_amount
            new_internal_balance = internal_balance + amount_in_rub

            # Обновляем базу данных
            cursor_users.execute(f'''
                UPDATE users SET {balance_field} = ?, internal_balance = ? WHERE id = ?
            ''', (new_external_balance, new_internal_balance, user_id))
            conn_users.commit()

            # Генерация транзакции
            unique_string = f"{user_id}{amount_in_rub}{time.time()}{random.randint(0, 999999)}"
            hash_id = hashlib.sha256(unique_string.encode()).hexdigest()

            cursor_trans = conn_trans.cursor()
            cursor_trans.execute('''
                INSERT INTO transactions (id, user_id, amount, currency, status) 
                VALUES (?, ?, ?, ?, ?)
            ''', (hash_id, user_id, transfer_amount, currency, 'completed'))
            conn_trans.commit()

            # Отправка сообщения в Kafka
            try:
                app.logger.info("Отправка сообщения о транзакции в Kafka")
                producer.send('transactions', value={'user_id': user_id, 'transaction_id': hash_id, 'amount': transfer_amount, 'currency': currency})
            except Exception as e:
                app.logger.error("Ошибка при отправке в Kafka: %s", str(e))
                return jsonify({'error': 'Ошибка при отправке данных в Kafka'}), 500


        return jsonify({
            'new_external_balance': new_external_balance,
            'new_internal_balance': new_internal_balance,
            'transaction_id': hash_id
        }), 200
    except Exception as e:
        app.logger.error("Ошибка перевода: %s", str(e))
        return jsonify({'error': 'Внутренняя ошибка сервера'}), 500

@app.route('/history')
def history():
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('login'))

    try:
        with sqlite3.connect('transactions.db') as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT id, amount, currency, status, timestamp FROM transactions WHERE user_id = ? ORDER BY timestamp DESC', (user_id,))
            transactions = cursor.fetchall()

        return render_template('history.html', transactions=transactions)
    except Exception as e:
        app.logger.error("Ошибка получения истории транзакций: %s", str(e))
        return jsonify({'error': 'Ошибка получения истории'}), 500

@app.route('/logout')
def logout():
    session.clear()  # Очистка сессии
    flash('Вы успешно вышли', 'info')  # Сообщение об успешном выходе
    return redirect(url_for('login'))  # Перенаправление на страницу логина


if __name__ == '__main__':
    init_db()  # Инициализация базы данных при запуске
    app.run(debug=True)
