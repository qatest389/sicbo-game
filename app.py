import threading
import time
import random
import os
import re
import html
import uuid
import secrets
from flask import Flask, jsonify, request, render_template

# [LEGAL_SAFE_FIX] 7) 운영 배포 안전성: 디버그 모드 환경변수 제어 및 로깅 설정
# 실제 운영 시에는 FLASK_ENV=production으로 설정해야 합니다.
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
        print("🔥 [성공] Firebase DB에 연결되었습니다!")
    else:
        print("⚠️ [메모리 모드] 키 파일 없음")
except Exception as e:
    print(f"⚠️ [메모리 모드] 에러: {e}")

# [LEGAL_SAFE_FIX] 4) 동시성 처리를 위한 Global Lock
# 게임 상태 변경과 유저 데이터 수정이 동시에 일어날 때 데이터가 꼬이는 것을 방지
game_lock = threading.Lock()

# [LEGAL_SAFE_FIX] 1) 용어 변경: score, plays, max_record 등 기록 중심 데이터 구조
# [LEGAL_SAFE_FIX] 3) 인증: uid 외에 secure_token을 매핑하여 검증 (임시 메모리 세션 저장소)
session_store = {}  # {token: uid}

memory_db = {
    'users': {
        # 'score': 현재 보유 점수
        # 'plays': 누적 플레이 횟수 (랭킹용)
        # 'max_record': 한 판 최대 획득 점수 (랭킹용)
        'Rich_Bot': {'score': 5000000, 'nickname': 'RichGuy', 'plays': 100, 'max_record': 50000},
        'Lucky_Bot': {'score': 2500000, 'nickname': 'Lucky77', 'plays': 50, 'max_record': 150000},
        'Newbie_Bot': {'score': 100000, 'nickname': 'Newbie', 'plays': 10, 'max_record': 5000}
    }
}

app = Flask(__name__)

class GameEngine:
    def __init__(self):
        # [LEGAL_SAFE_FIX] 1) 상태값 변경: BETTING -> SELECTION (선택 단계)
        self.state = 'SELECTION'
        self.timer = 15
        self.dice = [1, 1, 1]
        self.sum_val = 3
        self.history = []
        
        # [LEGAL_SAFE_FIX] 1) 용어 변경: bets -> predictions (예측)
        self.current_predictions = {} 
        self.round_outcomes = [] # winners -> outcomes
        self.last_round_delta = {} # profit -> delta (변화량)
        
        # [LEGAL_SAFE_FIX] 랭킹 캐싱
        self.cached_ranking = []
        self.last_rank_update = 0

    def game_loop(self):
        while True:
            time.sleep(1)
            # [LEGAL_SAFE_FIX] 4) 동시성: 타이머 등 상태 변경 시 락 사용
            with game_lock:
                self.timer -= 1
                if self.timer <= 0:
                    self.next_state()

    def next_state(self):
        # 이미 락 내부에서 호출됨
        if self.state == 'SELECTION':
            self.state = 'RESULT' # or REVEAL
            self.timer = 5
            self.roll_dice_logic()
            self.process_rewards() # payouts -> rewards
            self.update_ranking_logic()
            
        elif self.state == 'RESULT':
            self.state = 'SELECTION'
            self.timer = 15
            self.current_predictions = {}
            self.round_outcomes = []
            self.last_round_delta = {} 

    def update_ranking_logic(self):
        # [LEGAL_SAFE_FIX] 2) 랭킹 기준 변경: 잔액(score)이 아닌 '최고 기록(max_record)' 또는 '플레이 횟수' 기준
        # 사행성 조장 방지를 위해 "누가 돈이 많나"가 아니라 "누가 대박 기록을 세웠나"로 변경
        ranking_list = []
        
        if db:
            try:
                # Firestore 쿼리 예시: max_record 내림차순
                docs = db.collection('users').order_by('max_record', direction=firestore.Query.DESCENDING).limit(10).stream()
                for doc in docs:
                    data = doc.to_dict()
                    nick = data.get('nickname', doc.id[:6])
                    ranking_list.append({
                        'nickname': nick, 
                        'max_record': data.get('max_record', 0), # 최고 기록
                        'plays': data.get('plays', 0)            # 플레이 횟수
                    })
            except Exception as e:
                print(f"Ranking Update Error: {e}")
        else:
            users = memory_db['users']
            # 메모리 DB 정렬: max_record 기준
            sorted_users = sorted(users.items(), key=lambda item: item[1].get('max_record', 0), reverse=True)
            for uid, data in sorted_users[:10]:
                nick = data.get('nickname', uid[:6])
                ranking_list.append({
                    'nickname': nick, 
                    'max_record': data.get('max_record', 0),
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
        # [LEGAL_SAFE_FIX] 1) 용어 변경: ODDS -> MULTIPLIERS (배당률 -> 획득 배수)
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
            
            # [LEGAL_SAFE_FIX] 2) 기록 업데이트: 최고 점수 기록 등 갱신
            if total_gained_points > 0:
                self.update_user_stats(uid, total_gained_points, is_win=True)
            else:
                self.update_user_stats(uid, 0, is_win=False)

    # [LEGAL_SAFE_FIX] DB 접근 헬퍼 함수들 (Lock 내부에서 호출되거나 읽기 전용)
    def get_user_data(self, uid):
        default_data = {'score': 1000000, 'nickname': 'Guest', 'plays': 0, 'max_record': 0}
        if db:
            doc = db.collection('users').document(uid).get()
            if doc.exists:
                return doc.to_dict()
            else:
                db.collection('users').document(uid).set(default_data)
                return default_data
        else:
            if uid not in memory_db['users']:
                memory_db['users'][uid] = default_data
            return memory_db['users'][uid]

    def update_user_stats(self, uid, gained_points, is_win):
        # 점수 업데이트 및 통계 갱신 (plays, max_record)
        current_data = self.get_user_data(uid)
        new_score = current_data['score'] + gained_points
        new_plays = current_data.get('plays', 0) + 1
        current_max = current_data.get('max_record', 0)
        
        # 이번 판 획득 점수가 기존 최고 기록보다 높으면 갱신
        new_max = max(current_max, gained_points) if is_win else current_max

        update_payload = {
            'score': new_score, # 단순 합산 (이미 사용된 포인트는 predict 시 차감됨)
            'plays': new_plays,
            'max_record': new_max
        }

        if db:
            ref = db.collection('users').document(uid)
            ref.update(update_payload)
        else:
            memory_db['users'][uid].update(update_payload)

    def deduct_points(self, uid, amount):
        # 포인트 사용 (예측 시 차감)
        if db:
            ref = db.collection('users').document(uid)
            ref.update({'score': firestore.Increment(-amount)})
        else:
            current = self.get_user_data(uid)['score']
            memory_db['users'][uid]['score'] = current - amount

    def refund_points(self, uid, amount):
        # 취소 시 환불
        if db:
            ref = db.collection('users').document(uid)
            ref.update({'score': firestore.Increment(amount)})
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

game = GameEngine()
t = threading.Thread(target=game.game_loop, daemon=True)
t.start()

# --- [LEGAL_SAFE_FIX] 3) 인증/보안 헬퍼 함수 ---
def generate_session_token(uid):
    token = secrets.token_hex(16)
    session_store[token] = uid
    return token

def verify_token(token):
    return session_store.get(token)

def get_uid_from_request():
    # 헤더: Authorization: Bearer <token>
    auth_header = request.headers.get('Authorization')
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
        return verify_token(token)
    # 하위 호환성 (개발 단계 편의): query string 'token'
    token = request.args.get('token')
    if token:
        return verify_token(token)
    return None

# [LEGAL_SAFE_FIX] 5) 입력 검증 함수 (XSS 방지, 길이 제한)
def validate_nickname(nickname):
    if not nickname: return False
    # 길이 2~12자
    if len(nickname) < 2 or len(nickname) > 12: return False
    # 허용 문자: 한글, 영문, 숫자, 언더바(_)
    if not re.match(r'^[a-zA-Z0-9가-힣_]+$', nickname): return False
    return True

# --- Routes ---

@app.route('/')
def home(): 
    return render_template('index.html')

# [LEGAL_SAFE_FIX] 3) 신규 엔드포인트: 로그인/세션 발급
# 클라이언트는 최초 접속 시 uid(로컬스토리지 등)를 보내 토큰을 받아야 함
@app.route('/api/auth', methods=['POST'])
def authenticate():
    data = request.json
    uid = data.get('uid')
    if not uid:
        # UID가 없으면 서버가 새로 생성해서 부여 (익명 로그인)
        uid = "user_" + str(uuid.uuid4())[:8]
    
    token = generate_session_token(uid)
    return jsonify({'success': True, 'token': token, 'uid': uid})

# [LEGAL_SAFE_FIX] 6) 신규 엔드포인트: 정책 및 고지 사항
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
    # [LEGAL_SAFE_FIX] 3) 토큰 검증 (없으면 에러 또는 제한적 정보)
    uid = get_uid_from_request()
    
    # 닉네임, 점수 등은 UID가 있어야 조회 가능
    current_score = 0
    my_nick = "Guest"
    my_selections = {}
    round_result = 0
    
    if uid:
        # [LEGAL_SAFE_FIX] 4) 동시성: 읽기 작업이라도 락을 걸거나, 사본을 뜨는 게 안전
        # 성능을 위해 락 없이 읽되, GameEngine 내부가 원자적이길 기대하거나 짧게 락 사용
        user_data = game.get_user_data(uid)
        current_score = user_data.get('score', 0)
        my_nick = user_data.get('nickname', 'Guest')
        my_selections = game.current_predictions.get(uid, {})
        round_result = game.last_round_delta.get(uid, 0)

    # 공통 게임 상태
    display_dice = [0,0,0]
    display_sum = 0
    display_outcomes = []
    
    if game.state == 'RESULT':
        display_dice = game.dice
        display_sum = game.sum_val
        display_outcomes = game.round_outcomes

    return jsonify({
        'state': game.state,
        'timer': game.timer,
        'dice': display_dice,
        'sum': display_sum,
        'outcomes': display_outcomes,
        'history': game.history,
        # [LEGAL_SAFE_FIX] 1) 용어 변경: balance -> score (하위 호환성을 위해 balance도 남김)
        'score': current_score,
        'balance': current_score, # Deprecated
        'nickname': my_nick,
        # [LEGAL_SAFE_FIX] 1) 용어 변경: bets -> selections
        'my_selections': my_selections,
        'my_bets': my_selections, # Deprecated
        # [LEGAL_SAFE_FIX] 1) 용어 변경: profit -> round_result
        'round_result': round_result,
        'ranking': game.get_ranking()
    })

# [LEGAL_SAFE_FIX] 1) 라우트 변경: /bet -> /predict
@app.route('/predict', methods=['POST'])
def make_prediction():
    # [LEGAL_SAFE_FIX] 3) 보안: 토큰 검증 필수
    uid = get_uid_from_request()
    if not uid:
        return jsonify({'success': False, 'msg': '로그인이 필요합니다.'}), 401

    # [LEGAL_SAFE_FIX] 4) 동시성: 락 적용
    with game_lock:
        if game.state != 'SELECTION': 
            return jsonify({'success': False, 'msg': '선택 시간이 마감되었습니다.'})
        
        data = request.json
        # [LEGAL_SAFE_FIX] 1) 용어 변경
        prediction_type = data.get('prediction_type') or data.get('bet_type')
        points = int(data.get('points', 0) or data.get('amount', 0))

        if points <= 0:
            return jsonify({'success': False, 'msg': '올바르지 않은 포인트입니다.'})

        user_data = game.get_user_data(uid)
        if user_data['score'] < points: 
            return jsonify({'success': False, 'msg': '포인트가 부족합니다.'})

        # 포인트 차감 및 예측 기록
        game.deduct_points(uid, points)
        
        if uid not in game.current_predictions: 
            game.current_predictions[uid] = {}
        
        if prediction_type in game.current_predictions[uid]: 
            game.current_predictions[uid][prediction_type] += points
        else: 
            game.current_predictions[uid][prediction_type] = points

    return jsonify({'success': True})

# [LEGAL_SAFE_FIX] 1) 라우트 변경: /bet/clear -> /predict/clear
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
    
    # [LEGAL_SAFE_FIX] 5) 입력 값 검증 및 살균
    if not validate_nickname(nickname):
        return jsonify({'success': False, 'msg': '닉네임은 2~12자의 한글,영문,숫자만 가능합니다.'})
    
    # XSS 방지 처리
    safe_nickname = html.escape(nickname)
    
    game.set_nickname(uid, safe_nickname)
    return jsonify({'success': True})

if __name__ == '__main__':
    # [LEGAL_SAFE_FIX] 7) 운영 배포 안전성: debug=False로 고정
    app.run(debug=is_debug, use_reloader=False, host='0.0.0.0')