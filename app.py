from flask import Flask, render_template, request, redirect, url_for, jsonify, session
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from models import db, User, Chat, ChatMember, Message, PrivateMessage
from datetime import datetime
import uuid

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-here-change-in-production'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///forevo.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)
socketio = SocketIO(app, cors_allowed_origins="*")
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Словарь для хранения connection_id -> user_id
user_connections = {}

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(user_id)

# ---------- АУТЕНТИФИКАЦИЯ ----------
@app.route('/')
def index():
    if current_user.is_authenticated:
        return render_template('index.html')
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for('index'))
        return render_template('login.html', error='Неверный логин или пароль')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        if User.query.filter_by(username=username).first():
            return render_template('register.html', error='Пользователь уже существует')
        user = User(username=username, password=generate_password_hash(password))
        db.session.add(user)
        db.session.commit()
        login_user(user)
        return redirect(url_for('index'))
    return render_template('register.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# ---------- SOCKET.IO ----------
@socketio.on('connect')
def handle_connect():
    if current_user.is_authenticated:
        user_connections[request.sid] = current_user.id
        current_user.is_online = True
        db.session.commit()
        emit('user_status', {'user_id': current_user.id, 'is_online': True}, broadcast=True)
        print(f"✅ Пользователь {current_user.username} подключился")
        
        # Присоединяемся ко всем чатам
        for cm in ChatMember.query.filter_by(user_id=current_user.id).all():
            join_room(cm.chat_id)
            print(f"📁 Присоединился к чату {cm.chat_id}")

@socketio.on('disconnect')
def handle_disconnect():
    if current_user.is_authenticated:
        user_connections.pop(request.sid, None)
        current_user.is_online = False
        db.session.commit()
        emit('user_status', {'user_id': current_user.id, 'is_online': False}, broadcast=True)
        print(f"❌ Пользователь {current_user.username} отключился")

@socketio.on('send_message')
def handle_message(data):
    print(f"📨 Получено сообщение: {data}")
    chat_id = data.get('chat_id')
    content = data.get('content')
    
    if not chat_id or not content:
        print("❌ Нет chat_id или content")
        return
    
    # Проверяем, что пользователь в чате
    member = ChatMember.query.filter_by(chat_id=chat_id, user_id=current_user.id).first()
    if not member:
        print(f"❌ Пользователь не в чате {chat_id}")
        return
    
    message = Message(
        chat_id=chat_id,
        sender_id=current_user.id,
        content=content
    )
    db.session.add(message)
    db.session.commit()
    
    print(f"✅ Сообщение сохранено в чат {chat_id}")
    
    payload = {
        'id': message.id,
        'chat_id': chat_id,
        'content': content,
        'sender_id': current_user.id,
        'sender_name': current_user.username,
        'sent_at': message.sent_at.isoformat()
    }
    
    # Отправляем всем в комнате чата
    socketio.emit('new_message', payload, room=chat_id)
    print(f"📤 Отправлено в комнату {chat_id}: {payload}")

@socketio.on('send_private')
def handle_private(data):
    print(f"📨 Получено личное сообщение: {data}")
    receiver_id = data.get('receiver_id')
    content = data.get('content')
    
    if not receiver_id or not content:
        print("❌ Нет receiver_id или content")
        return
    
    message = PrivateMessage(
        sender_id=current_user.id,
        receiver_id=receiver_id,
        content=content
    )
    db.session.add(message)
    db.session.commit()
    
    print(f"✅ Личное сообщение сохранено")
    
    payload = {
        'id': message.id,
        'content': content,
        'sender_id': current_user.id,
        'sender_name': current_user.username,
        'receiver_id': receiver_id,
        'sent_at': message.sent_at.isoformat()
    }
    
    # Отправляем отправителю
    emit('new_private', payload, room=request.sid)
    
    # Отправляем получателю (если онлайн)
    receiver_sid = None
    for sid, uid in user_connections.items():
        if uid == receiver_id:
            receiver_sid = sid
            break
    
    if receiver_sid:
        emit('new_private', payload, room=receiver_sid)
        print(f"📤 Отправлено получателю {receiver_id}")
    else:
        print(f"❌ Получатель {receiver_id} не онлайн")

# ---------- API ДЛЯ ЧАТОВ ----------
@app.route('/api/users')
@login_required
def get_users():
    users = User.query.filter(User.id != current_user.id).all()
    return jsonify([{
        'id': u.id,
        'username': u.username,
        'is_online': u.is_online,
        'avatar_url': u.avatar_url
    } for u in users])

@app.route('/api/chats')
@login_required
def get_chats():
    chat_members = ChatMember.query.filter_by(user_id=current_user.id).all()
    chats = []
    for cm in chat_members:
        chat = cm.chat
        members = [{
            'id': m.user.id,
            'username': m.user.username,
            'is_online': m.user.is_online,
            'is_admin': m.is_admin
        } for m in chat.members]
        
        last_message = Message.query.filter_by(chat_id=chat.id).order_by(Message.sent_at.desc()).first()
        chats.append({
            'id': chat.id,
            'name': chat.name if chat.is_group else next((m['username'] for m in members if m['id'] != current_user.id), 'Чат'),
            'is_group': chat.is_group,
            'avatar_url': chat.avatar_url,
            'members': members,
            'last_message': {
                'content': last_message.content,
                'sent_at': last_message.sent_at.isoformat(),
                'sender_id': last_message.sender_id
            } if last_message else None
        })
    return jsonify(chats)

@app.route('/api/messages/<chat_id>')
@login_required
def get_messages(chat_id):
    messages = Message.query.filter_by(chat_id=chat_id).order_by(Message.sent_at.asc()).limit(100).all()
    return jsonify([{
        'id': m.id,
        'content': m.content,
        'sent_at': m.sent_at.isoformat(),
        'sender_id': m.sender_id,
        'sender_name': m.sender.username
    } for m in messages])

@app.route('/api/private/<user_id>')
@login_required
def get_private_messages(user_id):
    messages = PrivateMessage.query.filter(
        ((PrivateMessage.sender_id == current_user.id) & (PrivateMessage.receiver_id == user_id)) |
        ((PrivateMessage.sender_id == user_id) & (PrivateMessage.receiver_id == current_user.id))
    ).order_by(PrivateMessage.sent_at.asc()).limit(100).all()
    return jsonify([{
        'id': m.id,
        'content': m.content,
        'sent_at': m.sent_at.isoformat(),
        'sender_id': m.sender_id,
        'sender_name': User.query.get(m.sender_id).username
    } for m in messages])

@app.route('/api/start_chat/<user_id>', methods=['POST'])
@login_required
def start_private_chat(user_id):
    # Проверяем, существует ли уже личный чат
    existing = db.session.query(Chat).join(ChatMember).filter(
        ~Chat.is_group,
        ChatMember.user_id.in_([current_user.id, user_id])
    ).group_by(Chat.id).having(db.func.count(ChatMember.id) == 2).first()
    
    if existing:
        return jsonify({'chat_id': existing.id})
    
    # Создаём новый личный чат
    chat = Chat(is_group=False)
    db.session.add(chat)
    db.session.flush()
    
    db.session.add(ChatMember(chat_id=chat.id, user_id=current_user.id))
    db.session.add(ChatMember(chat_id=chat.id, user_id=user_id))
    db.session.commit()
    
    return jsonify({'chat_id': chat.id})

@app.route('/api/create_group', methods=['POST'])
@login_required
def create_group():
    data = request.json
    name = data.get('name')
    member_ids = data.get('members', [])
    
    chat = Chat(name=name, is_group=True)
    db.session.add(chat)
    db.session.flush()
    
    # Создатель - админ
    db.session.add(ChatMember(chat_id=chat.id, user_id=current_user.id, is_admin=True))
    for member_id in member_ids:
        if member_id != current_user.id:
            db.session.add(ChatMember(chat_id=chat.id, user_id=member_id))
    
    db.session.commit()
    
    # Оповещаем участников
    for member_id in member_ids + [current_user.id]:
        if member_id in user_connections.values():
            socketio.emit('chat_created', {
                'id': chat.id,
                'name': chat.name,
                'is_group': True
            }, room=[k for k, v in user_connections.items() if v == member_id][0])
    
    return jsonify({'chat_id': chat.id})


# ---------- ЗАПУСК ----------
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    socketio.run(app, debug=True, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)
