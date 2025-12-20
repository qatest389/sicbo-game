import threading
import time
import random
import os
import re
import html
import uuid
import secrets
from flask import Flask, jsonify, request, render_template, make_response

# 운영 환경 설정
is_debug = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'

# --- [Firebase 설정] ---
db = None
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    if os.path.exists('serviceAccountKey.json'):
        cred = credentials.Certificate('serviceAccountKey.json')
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        print("🔥 [성공] Firebase DB 연결됨")
    else:
        print("⚠️ [메모리 모드] 키 파일 없음")
except Exception as e:
    print(f"⚠️ [메모리 모드] 에러: {e}")

game_lock = threading.Lock()
session_store = {}

memory_db = {
    'users': {
        'Rich_Bot': {'score': 5000000, 'nickname': 'RichGuy', 'plays': 100, 'max_record': 50000},
        'Lucky_Bot': {'score': 2500000, 'nickname': 'Lucky77', 'plays': 50, 'max_record': 150000},
        'Newbie_Bot': {'score': 100000, 'nickname': 'Newbie', 'plays': 10, 'max_record': 5000}
    }
}

app = Flask(__name__)

class GameEngine:
    def __init__(self):
        self.state = 'SELECTION'
        # [FIX] 절대 시간 로직 도입
        self.duration = 15
        self.end_time = time.time() + self.duration # 목표 시간 설정
        
        self.dice = [1, 1, 1]
        self.sum_val = 3
        self.history = []
        self.current_predictions = {} 
        self.round_outcomes = [] 
        self.last_round_delta = {} 
        self.cached_ranking = []
        self.update_ranking_logic()

    def game_loop(self):
        """
        [FIX] sleep(1) 카운트 다운 방식 폐기 -> 절대 시간 비교 방식
        """
        print("🚀 [SYSTEM] Game Loop Started (Absolute Time Mode)")
        while True:
            time.sleep(0.5) # 0.5초마다 체크 (반응 속도 향상)
            
            try:
                now = time.time()
                # 목표 시간이 지났으면 다음 단계로 넘어감
                if now >= self.end_time:
                    with game_lock:
                        self.next_state()
            except Exception as e:
                print(f"🚨 [CRITICAL ERROR] Loop crashed: {e}")

    def get_remaining_time(self):
        # 남은 시간 = 목표 시간 - 현재 시간
        remaining = int(self.end_time - time.time())
        return max(0, remaining)

    def next_state(self):
        if self.state == 'SELECTION':
            self.state = 'RESULT' 
            self.duration = 5
            self.end_time = time.time() + self.duration
            
            self.roll_dice_logic()
            self.process_rewards()
            self.update_ranking_logic()
            
        elif self.state == 'RESULT':
            self.state = 'SELECTION'
            self.duration = 15
            self.end_time = time.time() + self.duration
            
            self.current_predictions = {}
            self.round_outcomes = []
            self.last_round_delta = {} 

    def update_ranking_logic(self):
        ranking_list = []
        if db:
            try:
                docs = db.collection('users').order_by('score', direction=firestore.Query.DESCENDING).limit(10).stream()
                for doc in docs:
                    data = doc.to_dict()
                    nick = data.get('nickname', doc.id[:6])
                    ranking_list.append({
                        'nickname': nick, 
                        'score': data.get('score', 0),
                        'plays': data.get('plays', 0)
                    })
            except Exception as e:
                print(f"Ranking Error: {e}")
        else:
            users = memory_db['users']
            sorted_users = sorted(users.items(), key=lambda item: item[1].get('score', 0), reverse=True)
            for uid, data in sorted_users[:10]:
                nick = data.get('nickname', uid[:6])
                ranking_list.append({
                    'nickname': nick, 
                    'score': data.get('score', 0),
                    'plays': data.get('plays', 0)
                })
        
        self.cached_ranking = ranking_list

    def get_ranking(self):
        return self.cached_ranking

    def roll_dice_logic(self):
        self.dice = [random.randint(1, 6) for _ in range(3)]
        self.sum_val = sum(self.dice)
        counts = {i: self.dice.count(i) for i in range(1, 7)}
        is_triple = (self.dice[0] == self.dice[1] == self.dice[2])
        is_any_double = any(c >= 2 for c in counts.values())

        outcomes = []
        if not is_triple:
            if 4 <= self.sum_val <= 10: outcomes.append('SMALL')
            if 11 <= self.sum_val <= 17: outcomes.append('BIG')
            if self.sum_val % 2 != 0: outcomes.append('ODD')
            else: outcomes.append('EVEN')
        
        outcomes.append(f'TOTAL_{self.sum_val}')
        if is_any_double: outcomes.append('ANY_DOUBLE')
        if is_triple:
            outcomes.append('ANY_TRIPLE')
            outcomes.append(f'TRIPLE_{self.dice[0]}')
        
        for num in range(1, 7):
            if counts[num] >= 1: outcomes.append(f'SINGLE_{num}')
            if counts[num] >= 2: outcomes.append(f'DOUBLE_{num}')
            
        unique_dice = sorted(list(set(self.dice)))
        for i in range(len(unique_dice)):
            for j in range(i + 1, len(unique_dice)):
                outcomes.append(f'COMBO_{unique_dice[i]}_{unique_dice[j]}')
        
        self.round_outcomes = outcomes
        self.history.insert(0, {'dice': self.dice})
        if len(self.history) > 100: self.history.pop()

    def process_rewards(self):
        MULTIPLIERS = { 'SMALL': 1, 'BIG': 1, 'ODD': 1, 'EVEN': 1, 'ANY_TRIPLE': 30, 'ANY_DOUBLE': 5, 'TRIPLE': 180, 'DOUBLE': 10, 'COMBO': 5 }
        TOTAL_MULTIPLIERS = {4:60, 5:30, 6:18, 7:12, 8:8, 9:6, 10:6, 11:6, 12:6, 13:8, 14:12, 15:18, 16:30, 17:60}

        for uid, predictions in self.current_predictions.items():
            total_used_points = sum(predictions.values())
            total_gained_points = 0
            
            for p_type, points in predictions.items():
                if p_type in self.round_outcomes:
                    mult = 1
                    if p_type.startswith('TOTAL'): mult = TOTAL_MULTIPLIERS.get(self.sum_val, 1)
                    elif p_type.startswith('TRIPLE_'): mult = 180
                    elif p_type.startswith('DOUBLE_'): mult = 10
                    elif p_type.startswith('SINGLE_'):
                        num = int(p_type.split('_')[1])
                        mult = self.dice.count(num)
                    elif p_type.startswith('COMBO'): mult = 5
                    else: mult = MULTIPLIERS.get(p_type, 1)
                    
                    total_gained_points += points + (points * mult)
            
            net_change = total_gained_points - total_used_points
            self.last_round_delta[uid] = net_change
            
            if total_gained_points > 0:
                self.update_user_stats(uid, total_gained_points, is_win=True)
            else:
                self.update_user_stats(uid, 0, is_win=False)

    def get_user_data(self, uid):
        default_data = {'score': 1000000, 'nickname': 'Guest', 'plays': 0, 'max_record': 0}
        if db:
            try:
                doc = db.collection('users').document(uid).get()
                if doc.exists: return doc.to_dict()
                else:
                    db.collection('users').document(uid).set(default_data)
                    return default_data
            except Exception as e:
                print(f"DB Read Error: {e}")
                return default_data
        else:
            if uid not in memory_db['users']: memory_db['users'][uid] = default_data
            return memory_db['users'][uid]

    def update_user_stats(self, uid, gained_points, is_win):
        try:
            current_data = self.get_user_data(uid)
            new_score = current_data['score'] + gained_points
            new_plays = current_data.get('plays', 0) + 1
            current_max = current_data.get('max_record', 0)
            new_max = max(current_max, gained_points) if is_win else current_max

            update_payload = {'score': new_score, 'plays': new_plays, 'max_record': new_max}

            if db:
                db.collection('users').document(uid).update(update_payload)
            else:
                memory_db['users'][uid].update(update_payload)
        except Exception as e:
            print(f"Stats Update Error: {e}")

    def deduct_points(self, uid, amount):
        if db:
            db.collection('users').document(uid).update({'score': firestore.Increment(-amount)})
        else:
            current = self.get_user_data(uid)['score']
            memory_db['users'][uid]['score'] = current - amount

    def refund_points(self, uid, amount):
        if db:
            db.collection('users').document(uid).update({'score': firestore.Increment(amount)})
        else:
            current = self.get_user_data(uid)['score']
            memory_db['users'][uid]['score'] = current + amount

    def set_nickname(self, uid, nickname):
        with game_lock:
            if db:
                ref = db.collection('users').document(uid)
                if not ref.get().exists:
                    ref.set({'score': 1000000, 'nickname': nickname, 'plays': 0, 'max_record': 0})
                else:
                    ref.update({'nickname': nickname})
            else:
                if uid not in memory_db['users']:
                    memory_db['users'][uid] = {'score': 1000000, 'nickname': nickname, 'plays': 0, 'max_record': 0}
                else:
                    memory_db['users'][uid]['nickname'] = nickname
            self.update_ranking_logic()

# 게임 엔진 시작
game = GameEngine()
t = threading.Thread(target=game.game_loop, daemon=True)
t.start()

# --- 인증/보안 ---
def generate_session_token(uid):
    token = secrets.token_hex(16)
    session_store[token] = uid
    return token

def verify_token(token):
    return session_store.get(token)

def get_uid_from_request():
    auth_header = request.headers.get('Authorization')
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
        return verify_token(token)
    return None

def validate_nickname(nickname):
    if not nickname: return False
    if len(nickname) < 2 or len(nickname) > 12: return False
    if not re.match(r'^[a-zA-Z0-9가-힣_]+$', nickname): return False
    return True

# --- Routes ---

@app.route('/')
def home(): 
    return render_template('index.html')

@app.route('/api/auth', methods=['POST'])
def authenticate():
    data = request.json
    uid = data.get('uid')
    if not uid: uid = "user_" + str(uuid.uuid4())[:8]
    token = generate_session_token(uid)
    return jsonify({'success': True, 'token': token, 'uid': uid})

@app.route('/policy')
def policy():
    return jsonify({
        'is_entertainment_only': True,
        'no_cashout': True,
        'no_transfer': True,
        'message': "본 서비스는 오락 목적의 게임입니다. 포인트는 현금 가치가 없으며 환전, 양도, 거래가 불가능합니다."
    })

@app.route('/status')
def get_status():
    uid = get_uid_from_request()
    current_score = 0
    my_nick = "Guest"
    my_selections = {}
    round_result = 0
    
    if uid:
        try:
            user_data = game.get_user_data(uid)
            current_score = user_data.get('score', 0)
            my_nick = user_data.get('nickname', 'Guest')
            my_selections = game.current_predictions.get(uid, {})
            round_result = game.last_round_delta.get(uid, 0)
        except:
            pass

    display_dice = [0,0,0]
    display_sum = 0
    display_outcomes = []
    
    if game.state == 'RESULT':
        display_dice = game.dice
        display_sum = game.sum_val
        display_outcomes = game.round_outcomes

    # [FIX] 계산된 남은 시간을 반환
    remaining_time = game.get_remaining_time()

    resp = make_response(jsonify({
        'state': game.state,
        'timer': remaining_time, # 변수값이 아닌 계산값 전달
        'dice': display_dice,
        'sum': display_sum,
        'outcomes': display_outcomes,
        'history': game.history,
        'score': current_score,
        'nickname': my_nick,
        'my_selections': my_selections,
        'round_result': round_result,
        'ranking': game.get_ranking()
    }))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp

@app.route('/predict', methods=['POST'])
def make_prediction():
    uid = get_uid_from_request()
    if not uid: return jsonify({'success': False, 'msg': '로그인이 필요합니다.'}), 401

    with game_lock:
        if game.state != 'SELECTION': 
            return jsonify({'success': False, 'msg': '선택 시간이 마감되었습니다.'})
        
        data = request.json
        prediction_type = data.get('prediction_type')
        try:
            points = int(data.get('points', 0))
        except:
            return jsonify({'success': False, 'msg': '잘못된 포인트입니다.'})

        if points <= 0: return jsonify({'success': False, 'msg': '올바르지 않은 포인트입니다.'})

        user_data = game.get_user_data(uid)
        if user_data['score'] < points: 
            return jsonify({'success': False, 'msg': '포인트가 부족합니다.'})

        game.deduct_points(uid, points)
        
        if uid not in game.current_predictions: 
            game.current_predictions[uid] = {}
        
        if prediction_type in game.current_predictions[uid]: 
            game.current_predictions[uid][prediction_type] += points
        else: 
            game.current_predictions[uid][prediction_type] = points

    return jsonify({'success': True})

@app.route('/predict/clear', methods=['POST'])
def clear_predictions():
    uid = get_uid_from_request()
    if not uid: return jsonify({'success': False, 'msg': '로그인 필요'}), 401

    with game_lock:
        if game.state != 'SELECTION': 
            return jsonify({'success': False, 'msg': '취소할 수 없습니다.'})
        
        user_predictions = game.current_predictions.get(uid, {})
        if not user_predictions: 
            return jsonify({'success': True, 'msg': '선택 내역 없음'})
        
        total_refund = sum(user_predictions.values())
        game.refund_points(uid, total_refund)
        del game.current_predictions[uid]
    
    return jsonify({'success': True})

@app.route('/user/nickname', methods=['POST'])
def change_nickname():
    uid = get_uid_from_request()
    if not uid: return jsonify({'success': False, 'msg': '로그인 필요'}), 401

    data = request.json
    nickname = data.get('nickname', '').strip()
    
    if not validate_nickname(nickname):
        return jsonify({'success': False, 'msg': '닉네임은 2~12자의 한글,영문,숫자만 가능합니다.'})
    
    safe_nickname = html.escape(nickname)
    game.set_nickname(uid, safe_nickname)
    return jsonify({'success': True})

if __name__ == '__main__':
    app.run(debug=is_debug, use_reloader=False, host='0.0.0.0')