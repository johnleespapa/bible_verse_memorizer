# -*- coding: utf-8 -*-
"""
웹 기반 성경 구절 암기 퀴즈 (단일 파일 Flask 앱)
- CSV 컬럼: Book, Chapter, Verse, Text
- 퀴즈 유형: (1) 책/장/절 맞추기(identify_ref)
          (2) 빈칸 채우기(cloze)
          (3) 객관식: 문구→참조(multiple_choice)
          (4) 구절 이어쓰기(continue_verse)
          (5) 객관식: 장절→문구(multiple_choice_text)
- 스킵: 오답으로 취급(통계/가중치 반영)
- 대시보드(사용자별): 책별 정답률, 회차별 점수(학습 제외), 유형별 정답률, 스킵 비율,
            오답 TOP20(시험+학습 포함, 스킵 포함, 참조: 책 장,절)
- 학습(무한) 모드: 무한 출제(오답/스킵↑ 정답↓, verseScores 반영),
            **랜덤 ↔ 많이틀린 것(가중치)** 번갈아 출제,
            오답/스킵은 **3~5문항 후 재출제(무작위)**, 즉시 정답 공개,
            **사용자 클릭 시 다음 문제 진행**, 각 문항 **자동 기록**, 사용자별 저장
            상단에 **정확도(%)=누적정답/시도** 표시
- 시험 종료 시 **자동 저장(저장 완료 보장, 사용자별)**
- 과거 시험: 세트(문항 자체)·점수 저장, **재시험 시 기존 세트 덮어쓰기 업데이트**
- 추가: TOP 20으로만 시험보기 / 틀린 문제만 재시험 / 과거 세트 삭제 / TOP20 항목 삭제/초기화
- 변경: 시험(퀴즈 시작)은 **순수 랜덤(균등)** 샘플링(참조 중복 금지)
- 평가: **띄어쓰기는 고려하지 않음** (문자 LCS 유사도)
- 표시: **시험 중에는 점수/스킵 숨김**, **학습 중에는 점수/스킵/정확도 표시**
- 인증: **간단 가입/로그인/로그아웃** 추가, **유저별** 통계/설정/가중치 분리 저장
- 구절 로딩: **기본적으로 서버의 verses.csv** 를 자동 로드(환경변수 VERSES_FILE로 경로 변경 가능),
            원하면 설정 화면에서 **서버 기본 다시 불러오기** 또는 **사용자 CSV 업로드**로 덮어쓰기 가능
- 요약 보강: **책별 최근 오답율(최근 100문항)** 표시
- 랭킹: **최근 10문제 이상 시험 5개 평균 점수** 기준 전체 유저 랭킹 표시(상위 10)
- 영속 저장: quiz_stats.json
의존성: Flask, Bootstrap/Chart.js/PapaParse, Werkzeug(비밀번호 해시)
실행: python bible_quiz_app.py → http://127.0.0.1:5000
"""
import os, json, csv
from flask import Flask, Response, request, jsonify, session
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("BIBLE_QUIZ_SECRET", "dev-secret-change-me")

# ------------------------------
# 서버측 영속 저장소 (멀티유저)
# ------------------------------
DATA_FILE = "quiz_stats.json"
DEFAULT_SETTINGS = {
    "numQuestions": 30,
    "enabledQTypes": ["identify_ref", "cloze", "multiple_choice", "continue_verse", "multiple_choice_text"]
}
DEFAULT_USER_DATA = {
    "sessions": [],
    "settings": DEFAULT_SETTINGS.copy(),
    "verseScores": {}  # key: "Book|Chapter|Verse" -> int (>=0)
}
DEFAULT_DB = {
    "users": {}  # username -> {"pw_hash": str, **DEFAULT_USER_DATA}
}

def load_db():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            db = json.load(f)
    except Exception:
        db = {}

    # 마이그레이션: 단일 사용자 스키마를 멀티유저로 승격
    if "users" not in db:
        users = {}
        sessions = db.get("sessions", [])
        settings = db.get("settings", DEFAULT_SETTINGS.copy())
        verseScores = db.get("verseScores", {})
        if sessions or verseScores or settings:
            users["_migrated"] = {"pw_hash": None, "sessions": sessions, "settings": settings, "verseScores": verseScores}
        db = {"users": users}

    # 필수 기본값 보정 (사용자 설정을 존중: 체크 해제 항목을 임의 추가하지 않음)
    for uname, u in list(db["users"].items()):
        if u is None or not isinstance(u, dict):
            db["users"][uname] = {"pw_hash": None, **DEFAULT_USER_DATA}
        else:
            u.setdefault("pw_hash", None)
            u.setdefault("sessions", [])
            # settings가 없으면 기본 제공
            if not isinstance(u.get("settings"), dict):
                u["settings"] = DEFAULT_SETTINGS.copy()
            else:
                u["settings"].setdefault("numQuestions", DEFAULT_SETTINGS["numQuestions"])
                u["settings"].setdefault("enabledQTypes", DEFAULT_SETTINGS["enabledQTypes"].copy())
            u.setdefault("verseScores", {})

    return db

def save_db(db):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

def ensure_user(db, username):
    if username not in db["users"]:
        db["users"][username] = {"pw_hash": None, **json.loads(json.dumps(DEFAULT_USER_DATA))}
    u = db["users"][username]
    u.setdefault("sessions", [])
    if not isinstance(u.get("settings"), dict):
        u["settings"] = DEFAULT_SETTINGS.copy()
    else:
        u["settings"].setdefault("numQuestions", DEFAULT_SETTINGS["numQuestions"])
        u["settings"].setdefault("enabledQTypes", DEFAULT_SETTINGS["enabledQTypes"].copy())
    u.setdefault("verseScores", {})
    return u

def current_username():
    return session.get("username")

def require_login():
    return bool(current_username())

# ------------------------------
# 서버 기본 구절 로딩(verses.csv)
# ------------------------------
SERVER_VERSES = []
SERVER_VERSES_SOURCE = None  # 실제 사용 파일 경로

def _parse_row_to_verse(row):
    # 헤더 대소문자/변형 허용
    def getcol(*names):
        for n in names:
            if n in row and row[n] is not None:
                return row[n]
        # 소문자 키 fallback
        lower = {k.lower(): v for k,v in row.items()}
        for n in names:
            ln = n.lower()
            if ln in lower and lower[ln] is not None:
                return lower[ln]
        return None

    book = (getcol("Book","book") or "").strip()
    ch = getcol("Chapter","chapter")
    vs = getcol("Verse","verse")
    text = (getcol("Text","text") or "").strip()
    try:
        ch = int(ch)
        vs = int(vs)
    except Exception:
        return None
    if not (book and isinstance(ch,int) and isinstance(vs,int) and text):
        return None
    return {"book": book, "chapter": ch, "verse": vs, "text": text}

def load_server_verses_file():
    """환경변수 VERSES_FILE 경로 우선, 없으면 ./verses.csv 시도"""
    global SERVER_VERSES, SERVER_VERSES_SOURCE
    paths = []
    env_path = os.environ.get("VERSES_FILE")
    if env_path:
        paths.append(env_path)
    paths.append(os.path.join(os.path.dirname(__file__), "verses.csv"))
    verses = []
    src = None
    for p in paths:
        try:
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8-sig", newline="") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        v = _parse_row_to_verse(row)
                        if v: verses.append(v)
                src = p
                break
        except Exception:
            try:
                with open(p, "r", encoding="utf-8", newline="") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        v = _parse_row_to_verse(row)
                        if v: verses.append(v)
                src = p
                break
            except Exception:
                continue
    SERVER_VERSES = verses
    SERVER_VERSES_SOURCE = src

# 앱 최초 기동 시 서버 기본 구절 로드
load_server_verses_file()

# ------------------------------
# HTML (Bootstrap + Chart.js + PapaParse)
# ------------------------------
INDEX_HTML = r"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>성경 암기 퀴즈</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/papaparse@5.4.1/papaparse.min.js"></script>
  <style>
    body { padding-bottom: 40px; }
    .hidden { display: none; }
    .nowrap { white-space: nowrap; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }
    .cloze-blank { border-bottom: 2px solid #999; padding: 0 6px; }
    .question-card.correct { border-left: 6px solid #28a745; }
    .question-card.incorrect { border-left: 6px solid #dc3545; }
    .answer-reveal { background: #f8f9fa; border-left: 4px solid #0d6efd; padding: .75rem 1rem; margin-top: .5rem; }
    .btn[disabled] { pointer-events: none; }
    .scroll-sm { max-height: 180px; overflow:auto; }
  </style>
</head>
<body>
  <nav class="navbar navbar-expand-lg bg-light border-bottom">
    <div class="container-fluid">
      <a class="navbar-brand fw-bold" href="#" onclick="showView('home')">성경 암기 퀴즈</a>

      <div class="d-flex flex-wrap gap-2 ms-2">
        <button class="btn btn-outline-primary btn-sm" onclick="showView('home')">대시보드</button>
        <button class="btn btn-primary btn-sm" id="btn-start-quiz">퀴즈 시작</button>
        <button class="btn btn-outline-danger btn-sm" id="btn-start-top20">TOP20 시험</button>
        <button class="btn btn-success btn-sm" id="btn-practice-toggle">학습하기 시작</button>
        <button class="btn btn-outline-secondary btn-sm" onclick="showView('settings')">설정/데이터</button>
      </div>

      <div class="ms-auto d-flex align-items-center gap-2" id="user-box">
        <span id="hello-user" class="me-2 hidden">안녕하세요, <strong id="current-username"></strong>님</span>
        <button class="btn btn-sm btn-outline-primary" id="btn-show-auth">로그인 / 가입</button>
        <button class="btn btn-sm btn-outline-dark hidden" id="btn-logout">로그아웃</button>
      </div>
    </div>
  </nav>

  <main class="container mt-4">
    <!-- AUTH -->
    <section id="view-auth" class="hidden">
      <div class="row justify-content-center">
        <div class="col-12 col-md-6">
          <div class="card">
            <div class="card-body">
              <ul class="nav nav-tabs mb-3" id="authTabs">
                <li class="nav-item"><button class="nav-link active" id="tab-login">로그인</button></li>
                <li class="nav-item"><button class="nav-link" id="tab-signup">가입</button></li>
              </ul>

              <div id="login-form">
                <div class="mb-2">
                  <label class="form-label">아이디</label>
                  <input type="text" class="form-control" id="login-username" />
                </div>
                <div class="mb-3">
                  <label class="form-label">비밀번호</label>
                  <input type="password" class="form-control" id="login-password" />
                </div>
                <button class="btn btn-primary w-100" id="btn-login">로그인</button>
              </div>

              <div id="signup-form" class="hidden">
                <div class="mb-2">
                  <label class="form-label">아이디</label>
                  <input type="text" class="form-control" id="signup-username" />
                </div>
                <div class="mb-3">
                  <label class="form-label">비밀번호</label>
                  <input type="password" class="form-control" id="signup-password" />
                </div>
                <button class="btn btn-success w-100" id="btn-signup">가입하기</button>
              </div>

            </div>
          </div>
          <div class="form-text mt-2">로그인하면 기록이 사용자별로 저장됩니다.</div>
        </div>
      </div>
    </section>

    <!-- HOME / DASHBOARD -->
    <section id="view-home" class="">
      <div class="row g-3">
        <div class="col-12 col-xl-4">
          <div class="card h-100">
            <div class="card-body">
              <h5 class="card-title">요약</h5>
              <ul class="list-unstyled mb-2" id="summary-list">
                <li>총 테스트 수: <span class="fw-bold" id="stat-total-tests">0</span></li>
                <li>최근 점수(30문항 만점): <span class="fw-bold" id="stat-last-score">-</span></li>
                <li>평균 점수: <span class="fw-bold" id="stat-avg-score">-</span></li>
                <li>주관식 정답률: <span class="fw-bold" id="stat-subj-acc">-</span></li>
                <li>객관식 정답률: <span class="fw-bold" id="stat-obj-acc">-</span></li>
                <li>스킵 비율: <span class="fw-bold" id="stat-skip-rate">-</span></li>
              </ul>
              <hr>
              <div>
                <div class="d-flex align-items-center justify-content-between">
                  <strong>책별 최근 오답율 <small class="text-muted">(최근 100문항)</small></strong>
                  <small id="recent-window-range" class="text-muted"></small>
                </div>
                <div class="scroll-sm mt-2">
                  <table class="table table-sm table-bordered align-middle mb-0">
                    <thead class="table-light">
                      <tr><th>책</th><th class="text-end">시도</th><th class="text-end">오답</th><th class="text-end">오답율</th></tr>
                    </thead>
                    <tbody id="table-recent-wrong-by-book"></tbody>
                  </table>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div class="col-12 col-xl-8">
          <div class="card h-100">
            <div class="card-body">
              <h5 class="card-title">회차별 점수</h5>
              <canvas id="chart-scores" height="120"></canvas>
            </div>
          </div>
        </div>

        <div class="col-12">
          <div class="card">
            <div class="card-body">
              <div class="d-flex align-items-center justify-content-between">
                <h5 class="card-title mb-0">과거 시험 (세트별 재시험)</h5>
                <small class="text-muted">낮은 점수 세트를 우선 재시험하세요</small>
              </div>
              <div class="table-responsive mt-2">
                <table class="table table-sm table-bordered align-middle mb-0">
                  <thead class="table-light">
                    <tr>
                      <th class="nowrap">세트</th>
                      <th class="nowrap">날짜</th>
                      <th class="nowrap text-end">문항</th>
                      <th class="nowrap text-end">점수</th>
                      <th class="nowrap text-end">정확도</th>
                      <th class="nowrap">액션</th>
                    </tr>
                  </thead>
                  <tbody id="table-past-sessions"></tbody>
                </table>
              </div>
            </div>
          </div>
        </div>

        <div class="col-12">
          <div class="card">
            <div class="card-body">
              <div class="d-flex align-items-center justify-content-between">
                <h5 class="card-title mb-0">오답 TOP 20 (시험+학습, 스킵 포함)</h5>
                <div class="d-flex gap-2">
                  <button class="btn btn-sm btn-outline-danger" id="btn-start-top20-2">TOP20 시험</button>
                  <button class="btn btn-sm btn-outline-secondary" id="btn-clear-top20">TOP20 초기화</button>
                </div>
              </div>
              <ul id="wrong-top" class="list-group mt-2"></ul>
            </div>
          </div>
        </div>

        <div class="col-12">
          <div class="card">
            <div class="card-body">
              <h5 class="card-title">전체 유저 랭킹 <small class="text-muted">(최근 10문항 이상 시험 5개 평균)</small></h5>
              <div class="table-responsive">
                <table class="table table-sm table-bordered align-middle mb-0">
                  <thead class="table-light">
                    <tr><th class="text-center">순위</th><th>사용자</th><th class="text-end">평균 점수(%)</th><th class="text-end">샘플수</th></tr>
                  </thead>
                  <tbody id="table-leaderboard"></tbody>
                </table>
              </div>
            </div>
          </div>
        </div>

      </div>
    </section>

    <!-- QUIZ -->
    <section id="view-quiz" class="hidden">
      <div class="card">
        <div class="card-body">
          <div class="d-flex flex-wrap gap-2 align-items-end">
            <div>
              <label class="form-label mb-1">한 회차 문항 수</label>
              <input id="input-num-questions" type="number" min="5" max="100" value="30" class="form-control form-control-sm" style="width:120px" />
            </div>
            <div>
              <label class="form-label mb-1">문제 유형</label>
              <div class="d-flex flex-wrap gap-3">
                <div class="form-check">
                  <input class="form-check-input" type="checkbox" value="identify_ref" id="qtype-identify" checked />
                  <label class="form-check-label" for="qtype-identify">책/장/절 맞추기</label>
                </div>
                <div class="form-check">
                  <input class="form-check-input" type="checkbox" value="cloze" id="qtype-cloze" checked />
                  <label class="form-check-label" for="qtype-cloze">빈칸 채우기</label>
                </div>
                <div class="form-check">
                  <input class="form-check-input" type="checkbox" value="multiple_choice" id="qtype-mc" checked />
                  <label class="form-check-label" for="qtype-mc">객관식(문구→참조)</label>
                </div>
                <div class="form-check">
                  <input class="form-check-input" type="checkbox" value="continue_verse" id="qtype-continue" checked />
                  <label class="form-check-label" for="qtype-continue">구절 이어쓰기</label>
                </div>
                <div class="form-check">
                  <input class="form-check-input" type="checkbox" value="multiple_choice_text" id="qtype-mc-text" checked />
                  <label class="form-check-label" for="qtype-mc-text">객관식(장절→문구)</label>
                </div>
              </div>
            </div>
            <div class="ms-auto">
              <button class="btn btn-outline-secondary" onclick="saveSettings()">설정 저장</button>
            </div>
          </div>
        </div>
      </div>

      <div id="quiz-panel" class="mt-3 hidden">
        <div class="d-flex justify-content-between align-items-center mb-2">
          <div>
            문항 <span id="cur-index">0</span>/<span id="total-count">0</span>
            <span id="mode-badge" class="badge text-bg-secondary ms-2" style="display:none;">학습 모드</span>
          </div>
          <div class="fw-bold">
            <span id="score-wrap">점수: <span id="cur-score">0</span></span>
            <span id="skip-wrap" class="ms-2 text-muted">스킵: <span id="cur-skip">0</span></span>
            <span id="practice-acc" class="ms-3 text-primary" style="display:none;">정확도: 0% (0/0)</span>
          </div>
        </div>
        <div id="question-box" class="card">
          <div class="card-body" id="question-content"></div>
        </div>
        <div class="mt-3 d-flex justify-content-between">
          <button class="btn btn-outline-secondary" onclick="prevQuestion()" id="btn-prev">이전</button>
          <div class="d-flex gap-2">
            <button class="btn btn-warning" onclick="skipQuestion()" id="btn-skip">스킵</button>
            <button class="btn btn-primary" onclick="submitAnswer()" id="btn-submit">답안 제출</button>
          </div>
          <button class="btn btn-outline-secondary" onclick="nextQuestion()" id="btn-next">다음</button>
        </div>
      </div>

      <div id="result-panel" class="mt-3 hidden">
        <div class="alert alert-info"><strong>회차 결과</strong> — 총 <span id="res-total"></span>문항, 정답 <span id="res-correct"></span>, 스킵 <span id="res-skip"></span>, 점수 <span id="res-score"></span></div>
        <div id="result-list" class="row g-2"></div>
        <div class="mt-3 d-flex gap-2">
          <button class="btn btn-outline-primary" onclick="showView('home'); buildDashboard()">대시보드로</button>
        </div>
      </div>
    </section>

    <!-- SETTINGS / DATA -->
    <section id="view-settings" class="hidden">
      <div class="card">
        <div class="card-body">
          <h5 class="card-title">CSV 업로드</h5>
          <p class="text-muted small mb-2">CSV 컬럼 순서: <span class="mono">Book,Chapter,Verse,Text</span> (헤더 포함 권장)</p>
          <input type="file" id="csv-input" accept=".csv" class="form-control" />
          <div class="form-text">파일은 서버에 업로드되지 않으며 브라우저에서만 파싱됩니다.</div>
          <div class="d-flex gap-2 mt-2">
            <button class="btn btn-sm btn-outline-primary" id="btn-load-server-verses">서버 기본 구절 다시 불러오기</button>
          </div>
        </div>
      </div>
      <div class="card mt-3">
        <div class="card-body">
          <div class="d-flex align-items-center justify-content-between">
            <h5 class="card-title mb-0">데이터 상태</h5>
            <div class="d-flex gap-2">
              <button class="btn btn-sm btn-outline-secondary" onclick="loadSettings()">서버 설정 불러오기</button>
              <button class="btn btn-sm btn-outline-danger" onclick="clearAll()">서버 기록 초기화</button>
            </div>
          </div>
          <div class="mt-2">
            <div>구절 수(메모리): <span id="meta-verse-count">0</span></div>
            <div>저장된 회차 기록(서버): <span id="meta-saved-sessions">0</span></div>
            <div>데이터 소스: <span class="badge text-bg-info" id="meta-source">-</span></div>
          </div>
        </div>
      </div>
    </section>
  </main>

  <script>
    // ------------------------------
    // 전역 상태(클라이언트)
    // ------------------------------
    let VERSES = []; // {book, chapter, verse, text}
    let VERSES_SOURCE = 'server'; // 'server' | 'upload'
    let CURRENT_QUIZ = null; // {questions, index, score, skip, answered[], practice, ...}
    let LAST_RESULT = null; // 서버 저장용 캐시
    let PRACTICE_MODE = false;
    let chartScores = null;
    let CURRENT_USER = null;

    // ------------------------------
    // 서버 API
    // ------------------------------
    async function apiGet(path){
      const r = await fetch(path);
      if (r.status === 401){
        alert('로그인이 필요합니다.');
        showView('auth');
        return {ok:false, error:'unauthorized'};
      }
      return await r.json();
    }
    async function apiPost(path, data){
      const r = await fetch(path, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(data||{})});
      if (r.status === 401){
        alert('로그인이 필요합니다.');
        showView('auth');
        return {ok:false, error:'unauthorized'};
      }
      return await r.json();
    }

    // ------------------------------
    // 서버 verses.csv 기본 로드
    // ------------------------------
    async function loadDefaultVersesFromServer(forceReload=false){
      const url = forceReload ? '/verses?reload=1' : '/verses';
      const res = await apiGet(url);
      if (res && res.ok){
        VERSES = res.verses || [];
        VERSES_SOURCE = 'server';
        refreshMeta();
        if ((res.meta?.count||0) > 0 && forceReload){
          alert('서버 기본 구절 로딩 완료: ' + res.meta.count + '개');
        }
      } else {
        console.warn('서버 구절 로드 실패', res?.error);
      }
    }

    // ------------------------------
    // 인증 UI
    // ------------------------------
    async function refreshWhoAmI(){
      const info = await apiGet('/whoami');
      if (!info) return;
      CURRENT_USER = info.logged_in ? info.username : null;
      updateAuthUI();
      // ★ 로그인되어 있으면 저장된 서버 설정을 즉시 불러와 UI에 반영
      if (CURRENT_USER){
        await loadSettings();
      }
    }
    function updateAuthUI(){
      const hello = document.getElementById('hello-user');
      const nameEl = document.getElementById('current-username');
      const btnAuth = document.getElementById('btn-show-auth');
      const btnLogout = document.getElementById('btn-logout');
      if (CURRENT_USER){
        if (hello) hello.classList.remove('hidden');
        if (nameEl) nameEl.textContent = CURRENT_USER;
        if (btnLogout) btnLogout.classList.remove('hidden');
        if (btnAuth) btnAuth.classList.add('hidden');
      } else {
        if (hello) hello.classList.add('hidden');
        if (nameEl) nameEl.textContent = '';
        if (btnLogout) btnLogout.classList.add('hidden');
        if (btnAuth) btnAuth.classList.remove('hidden');
      }
    }
    function bindAuthUI(){
      const tabLogin = document.getElementById('tab-login');
      const tabSignup = document.getElementById('tab-signup');
      const loginForm = document.getElementById('login-form');
      const signupForm = document.getElementById('signup-form');
      if (tabLogin) tabLogin.addEventListener('click', ()=>{ tabLogin.classList.add('active'); tabSignup.classList.remove('active'); loginForm.classList.remove('hidden'); signupForm.classList.add('hidden'); });
      if (tabSignup) tabSignup.addEventListener('click', ()=>{ tabSignup.classList.add('active'); tabLogin.classList.remove('active'); signupForm.classList.remove('hidden'); loginForm.classList.add('hidden'); });

      const btnAuth = document.getElementById('btn-show-auth');
      if (btnAuth) btnAuth.addEventListener('click', ()=>{ showView('auth'); });

      const btnLogout = document.getElementById('btn-logout');
      if (btnLogout) btnLogout.addEventListener('click', async ()=>{
        const res = await apiPost('/logout', {});
        if (res && res.ok){
          CURRENT_USER = null;
          updateAuthUI();
          showView('auth');
          buildDashboard(); // 비로그인 상태의 빈 대시보드
        }
      });

      const btnLogin = document.getElementById('btn-login');
      if (btnLogin) btnLogin.addEventListener('click', async ()=>{
        const username = document.getElementById('login-username').value.trim();
        const password = document.getElementById('login-password').value;
        if (!username || !password){ alert('아이디와 비밀번호를 입력하세요.'); return; }
        const res = await apiPost('/login', {username, password});
        if (res && res.ok){
          CURRENT_USER = res.username;
          updateAuthUI();
          showView('home');
          await loadSettings(); // ★ 로그인 직후 설정 반영
          buildDashboard();
        } else {
          alert(res?.error || '로그인 실패');
        }
      });

      const btnSignup = document.getElementById('btn-signup');
      if (btnSignup) btnSignup.addEventListener('click', async ()=>{
        const username = document.getElementById('signup-username').value.trim();
        const password = document.getElementById('signup-password').value;
        if (!username || !password){ alert('아이디와 비밀번호를 입력하세요.'); return; }
        const res = await apiPost('/signup', {username, password});
        if (res && res.ok){
          CURRENT_USER = res.username;
          updateAuthUI();
          showView('home');
          await loadSettings(); // ★ 가입 직후 설정 반영
          buildDashboard();
        } else {
          alert(res?.error || '가입 실패');
        }
      });
    }

    // ------------------------------
    // 버튼 바인딩
    // ------------------------------
    document.getElementById('btn-start-quiz').addEventListener('click', ()=>{ startQuizInternal(); });
    document.getElementById('btn-practice-toggle').addEventListener('click', ()=>{ togglePractice(); });
    document.getElementById('btn-start-top20').addEventListener('click', ()=>{ startTop20Quiz(); });
    document.getElementById('btn-start-top20-2').addEventListener('click', ()=>{ startTop20Quiz(); });
    document.getElementById('btn-load-server-verses').addEventListener('click', async ()=>{ await loadDefaultVersesFromServer(true); });
    document.getElementById('btn-clear-top20').addEventListener('click', async ()=>{
      if (!CURRENT_USER){ alert('로그인하세요.'); return; }
      if (!confirm('TOP20(오답 카운트)을 모두 초기화할까요?')) return;
      const res = await apiPost('/clear_top20', {});
      if (res && res.ok){ buildDashboard(); }
    });

    // ------------------------------
    // 뷰 전환
    // ------------------------------
    function showView(name){
      for (const id of ['home','quiz','settings','auth']){
        const el = document.getElementById('view-'+id);
        if (el) el.classList.toggle('hidden', id!==name);
      }
      if (name==='home') buildDashboard();
      if (name==='settings') refreshMeta();
      if (name==='auth') { /* no-op */ }
    }

    // ------------------------------
    // CSV 로딩 (클라이언트 파싱)
    // ------------------------------
    document.getElementById('csv-input').addEventListener('change', (ev)=>{
      const file = ev.target.files?.[0];
      if (!file) return;
      Papa.parse(file, {
        header: true,
        skipEmptyLines: true,
        complete: function(results){
          const rows = results.data; VERSES = [];
          for (const r of rows){
            const book = (r.Book || r.book || '').trim();
            const chapter = parseInt(r.Chapter || r.chapter, 10);
            const verse = parseInt(r.Verse || r.verse, 10);
            const text = (r.Text || r.text || '').trim();
            if (book && Number.isFinite(chapter) && Number.isFinite(verse) && text){ VERSES.push({book, chapter, verse, text}); }
          }
          VERSES_SOURCE = 'upload';
          alert('구절 로딩 완료: '+VERSES.length+'개 (사용자 업로드)');
          refreshMeta();
        }
      });
    });

    function refreshMeta(){
      const srcEl = document.getElementById('meta-source');
      document.getElementById('meta-verse-count').textContent = VERSES.length;
      apiGet('/data').then(d=>{ document.getElementById('meta-saved-sessions').textContent = (d && d.sessions) ? d.sessions.length : 0; });
      if (srcEl){
        srcEl.textContent = (VERSES_SOURCE==='upload') ? '사용자 업로드' : '서버 verses.csv';
        srcEl.className = 'badge ' + ((VERSES_SOURCE==='upload')?'text-bg-warning':'text-bg-info');
      }
    }

    // ------------------------------
    // 유틸리티 (정규화/유사도)
    // ------------------------------
    function randInt(n){ return Math.floor(Math.random()*n); }
    function shuffle(a){ for(let i=a.length-1;i>0;i--){ const j=randInt(i+1); [a[i],a[j]]=[a[j],a[i]]; } return a; }

    function normalizeText(s){
      if (!s) return '';
      const lowered = s.normalize('NFKC').toLowerCase();
      const noPunct = lowered.replace(/[\u2000-\u206F\u2E00-\u2E7F\\'!"#$%&()*+,\-./:;<=>?@\[\]^_`{|}~]/g, ' ');
      return noPunct.replace(/\s+/g,' ').trim();
    }
    function normalizeLabel(s){
      if (!s) return '';
      return s.normalize('NFKC').trim().toLowerCase().replace(/\s+/g,' ');
    }

    // 공백 완전 무시 비교용 정규화 + LCS
    function normalizeForCompare(s){
      if (!s) return '';
      const lowered = s.normalize('NFKC').toLowerCase();
      const noPunct = lowered.replace(/[\u2000-\u206F\u2E00-\u2E7F\\'!"#$%&()*+,\-./:;<=>?@\[\]^_`{|}~]/g, '');
      return noPunct.replace(/\s+/g,'');
    }
    function lcsLen(a, b){
      const n = a.length, m = b.length;
      if (n===0 || m===0) return 0;
      const prev = new Array(m+1).fill(0);
      const curr = new Array(m+1).fill(0);
      for (let i=1;i<=n;i++){
        for (let j=1;j<=m;j++){
          if (a[i-1] === b[j-1]) curr[j] = prev[j-1] + 1;
          else curr[j] = Math.max(prev[j], curr[j-1]);
        }
        for (let j=0;j<=m;j++) prev[j] = curr[j], curr[j] = 0;
      }
      return prev[m];
    }
    function charSimIgnoreSpaces(a, b){
      const A = normalizeForCompare(a);
      const B = normalizeForCompare(b);
      const L = Math.max(A.length, B.length);
      if (L === 0) return 0;
      const lcs = lcsLen(A, B);
      return lcs / L;
    }

    function formatRef(v){ return `${v.book} ${v.chapter},${v.verse}`; }
    function verseKey(v){ return `${v.book}|${v.chapter}|${v.verse}`; } // 참조 키

    // ------------------------------
    // 출제 가중치 (오답↑ 스킵↑ 정답↓ + verseScores 강화) — 학습 모드에서만 사용
    // ------------------------------
    function buildWeights(sessions, verseScores){
      const statByRef = {};
      for (const se of (sessions||[])){
        for (const d of (se.details||[])){
          const key = `${d.book}|${d.chapter}|${d.verse}`;
          if (!statByRef[key]) statByRef[key] = {correct:0, wrong:0, skip:0};
          if (d.correct) statByRef[key].correct++;
          else { if (d.skipped) statByRef[key].skip++; else statByRef[key].wrong++; }
        }
      }
      // 가중치 파라미터
      const BASE=1, A=1, S=1, C=1, V=4;

      const weights = new Array(VERSES.length).fill(1);
      for (let i=0;i<VERSES.length;i++){
        const v = VERSES[i];
        const key = verseKey(v);
        const s = statByRef[key] || {correct:0, wrong:0, skip:0};
        const vs = (verseScores && typeof verseScores[key]==='number') ? verseScores[key] : 0;
        let w = BASE + A*s.wrong + S*s.skip - C*s.correct + V*vs;
        if (w < 1) w = 1;
        weights[i] = w;
      }
      const prefix = [];
      let acc=0;
      for (let i=0;i<weights.length;i++){ acc+=weights[i]; prefix.push(acc); }
      return { weights, prefix, total: acc };
    }

    function pickVerseWeighted(prefix, total){
      if (!VERSES.length) return null;
      if (total<=0) return VERSES[randInt(VERSES.length)];
      const r = Math.random()*total;
      let lo=0, hi=prefix.length-1, ans=hi;
      while (lo<=hi){
        const mid = (lo+hi)>>1;
        if (prefix[mid] >= r){ ans=mid; hi=mid-1; }
        else lo=mid+1;
      }
      return VERSES[ans];
    }

    // (시험 전용) 균등 랜덤 비복원 샘플링 + 참조 유니크 보장
    function pickUniqueIndicesUniform(want){
      const idxs = Array.from({length: VERSES.length}, (_,i)=>i);
      shuffle(idxs); // 균등 랜덤
      const selected = [];
      const usedRef = new Set();
      for (const i of idxs){
        const v = VERSES[i];
        const k = verseKey(v);
        if (usedRef.has(k)) continue; // 같은 참조 재출제 금지
        usedRef.add(k);
        selected.push(i);
        if (selected.length === want) break;
      }
      return selected;
    }

    // ------------------------------
    // 학습 모드 전용: 선택 로직 초기화 (랜덤 ↔ 가중치 번갈아 + 재출제 큐 + 최근 버퍼)
    // ------------------------------
    function initPracticeSelector(dataForWeight, qtypes){
      const {prefix, total} = buildWeights(dataForWeight.sessions, dataForWeight.verseScores);
      const recentKeys = [];           // 최근 N개 버퍼
      const RECENT_MAX = 10;
      const retryQueue = [];           // {key, verse, dueAt, tries}
      let altToggle = false;           // false: 균등 랜덤, true: 가중치
      let attemptsCounter = 0;         // 현재까지 시도 수(학습 모드)

      function onAttempt(){ attemptsCounter++; }

      function pushRecent(key){
        recentKeys.push(key);
        if (recentKeys.length > RECENT_MAX) recentKeys.shift();
      }
      function inRecent(key){ return recentKeys.includes(key); }

      function pickUniformAvoidRecent(maxTries=50){
        if (!VERSES.length) return null;
        for (let t=0;t<maxTries;t++){
          const v = VERSES[randInt(VERSES.length)];
          const k = verseKey(v);
          if (!inRecent(k)) return v;
        }
        return VERSES[randInt(VERSES.length)]; // fallback
      }

      function pickWeightedAvoidRecent(maxTries=80){
        if (!VERSES.length) return null;
        for (let t=0;t<maxTries;t++){
          const v = pickVerseWeighted(prefix, total);
          const k = verseKey(v);
          if (!inRecent(k)) return v;
        }
        return pickVerseWeighted(prefix, total); // fallback
      }

      function scheduleRetryForVerse(verse, currentAttempts){
        const k = verseKey(verse);
        const found = retryQueue.find(it => it.key === k);
        const gap = 3 + randInt(3); // 3~5문항 뒤 무작위
        const due = (currentAttempts||0) + gap;
        if (found){
          found.dueAt = Math.min(found.dueAt, due);
          found.tries = (found.tries||0) + 1;
        } else {
          retryQueue.push({ key: k, verse, dueAt: due, tries: 1 });
        }
      }

      function discardFromRetryByKey(k){
        const idx = retryQueue.findIndex(it => it.key === k);
        if (idx >= 0) retryQueue.splice(idx,1);
      }

      function chooseVerse(){
        // 1) 재출제 큐 우선
        const idx = retryQueue.findIndex(it => it.dueAt <= attemptsCounter);
        if (idx >= 0){
          const it = retryQueue.splice(idx,1)[0];
          pushRecent(it.key);
          return it.verse;
        }
        // 2) 번갈이: false면 랜덤, true면 가중치
        let verse = null;
        if (altToggle){
          verse = pickWeightedAvoidRecent();
        } else {
          verse = pickUniformAvoidRecent();
        }
        const k = verseKey(verse);
        pushRecent(k);
        altToggle = !altToggle;
        return verse;
      }

      function makeOne(){
        const verse = chooseVerse();
        const t = qtypes[randInt(qtypes.length)];
        return makeQuestion(t, verse);
      }

      return {
        makeOne, retryQueue, recentKeys,
        scheduleRetryForVerse, discardFromRetryByKey,
        onAttempt
      };
    }

    // ------------------------------
    // 설정 I/O
    // ------------------------------
    async function loadSettings(){
      const d = await apiGet('/data');
      const st = d.settings || { numQuestions:30, enabledQTypes:["identify_ref","cloze","multiple_choice","continue_verse","multiple_choice_text"] };
      document.getElementById('input-num-questions').value = st.numQuestions;
      document.getElementById('qtype-identify').checked = st.enabledQTypes.includes('identify_ref');
      document.getElementById('qtype-cloze').checked = st.enabledQTypes.includes('cloze');
      document.getElementById('qtype-mc').checked = st.enabledQTypes.includes('multiple_choice');
      document.getElementById('qtype-continue').checked = st.enabledQTypes.includes('continue_verse');
      // ★ 버그 수정: 저장값을 그대로 반영 (자동 강제 체크 금지)
      document.getElementById('qtype-mc-text').checked = st.enabledQTypes.includes('multiple_choice_text');
      // 알림은 최초 자동 로드시엔 표시 안함(사용자 클릭 시에만 표시)
      return st;
    }

    async function saveSettings(){
      if (!CURRENT_USER){ alert('설정을 저장하려면 로그인하세요.'); showView('auth'); return; }
      const st = {
        numQuestions: Math.max(5, Math.min(100, parseInt(document.getElementById('input-num-questions').value,10)||30)),
        enabledQTypes: [
          ...(document.getElementById('qtype-identify').checked? ['identify_ref']:[]),
          ...(document.getElementById('qtype-cloze').checked? ['cloze']:[]),
          ...(document.getElementById('qtype-mc').checked? ['multiple_choice']:[]),
          ...(document.getElementById('qtype-continue').checked? ['continue_verse']:[]),
          ...(document.getElementById('qtype-mc-text').checked? ['multiple_choice_text']:[]),
        ]
      };
      const res = await apiPost('/settings', st);
      if (res && res.ok) alert('설정을 서버에 저장했습니다.');
    }

    // ------------------------------
    // 문제 생성기
    // ------------------------------
    function makeQuestion(type, verse){
      if (type==='identify_ref'){
        return { qtype:'identify_ref', subj:true, verse, prompt:'다음 구절의 책/장/절을 입력하세요:' };
      } else if (type==='cloze'){
        const masked = maskWords(verse.text, 2 + randInt(2));
        return { qtype:'cloze', subj:true, verse,
          prompt:`${formatRef(verse)} — 빈칸에 들어갔던 단어를 복원하세요:`,
          maskedHtml: masked.html, answers: masked.answers };
      } else if (type==='continue_verse'){
        return { qtype:'continue_verse', subj:true, verse, prompt:`${formatRef(verse)} — 구절 전체를 입력하세요:` };
      } else if (type==='multiple_choice_text'){
        const options = [verse];
        while (options.length<4){
          const candidate = VERSES[randInt(VERSES.length)];
          const dupRef = options.some(o=>o.book===candidate.book && o.chapter===candidate.chapter && o.verse===candidate.verse);
          const dupText = options.some(o=>o.text===candidate.text);
          if (!dupRef && !dupText){ options.push(candidate); }
        }
        shuffle(options);
        const correctIndex = options.findIndex(o=>o.text===verse.text && o.book===verse.book && o.chapter===verse.chapter && o.verse===verse.verse);
        return {
          qtype:'multiple_choice_text', subj:false, verse,
          prompt:`다음 참조의 올바른 문구를 고르세요: ${formatRef(verse)}`,
          options: options.map(o=>`“${o.text}”`),
          correctIndex
        };
      } else { // multiple_choice (문구→참조)
        const options = [verse];
        while (options.length<4){
          const candidate = VERSES[randInt(VERSES.length)];
          if (!options.some(o=>o.book===candidate.book && o.chapter===candidate.chapter && o.verse===candidate.verse)){
            options.push(candidate);
          }
        }
        shuffle(options);
        const correctIndex = options.findIndex(o=>o.book===verse.book && o.chapter===verse.chapter && o.verse===verse.verse);
        return {
          qtype:'multiple_choice', subj:false, verse,
          prompt:`다음 구절의 참조(책/장/절)를 고르세요: “${verse.text}”`,
          options: options.map(o=>`${o.book} ${o.chapter},${o.verse}`),
          correctIndex
        };
      }
    }

    function maskWords(text, n=2){
      const words = text.split(/(\s+)/); // 공백 유지
      const idx = []
      for (let i=0;i<words.length;i+=2){ if (words[i].trim().length>2) idx.push(i); }
      shuffle(idx)
      const chosen = idx.slice(0, Math.min(n, idx.length));
      const blanks = [];
      for (const i of chosen){ blanks.push(words[i]); words[i] = '<span class="cloze-blank">____</span>'; }
      return {html: words.join(''), answers: blanks};
    }

    // ------------------------------
    // 점수표시 토글
    // ------------------------------
    function setScoreVisibility(show){
      const sw = document.getElementById('score-wrap');
      const skw = document.getElementById('skip-wrap');
      if (sw) sw.style.display = show ? '' : 'none';
      if (skw) skw.style.display = show ? '' : 'none';
    }

    // ------------------------------
    // 퀴즈(시험) 시작 / 재시험 시작 / TOP20 시작
    // ------------------------------
    async function startQuizInternal(){
      if (!CURRENT_USER){ alert('시험을 시작하려면 로그인하세요.'); showView('auth'); return; }
      if (VERSES.length === 0){
        await loadDefaultVersesFromServer(true);
        if (VERSES.length === 0){ alert('서버 verses.csv를 찾을 수 없습니다. 설정에서 CSV를 업로드하세요.'); showView('settings'); return; }
      }

      // 유니크 참조 수 산출
      const uniqRefSet = new Set(VERSES.map(v=>verseKey(v)));
      const uniqCapacity = uniqRefSet.size;

      const d = await apiGet('/data');
      const st = d.settings || {};
      const desired = Math.max(5, Math.min(100, parseInt(document.getElementById('input-num-questions').value,10) || st.numQuestions || 30));
      const num = Math.min(desired, uniqCapacity); // 세트 크기는 유니크 참조 수를 넘지 않음

      const qtypes = [];
      if (document.getElementById('qtype-identify').checked) qtypes.push('identify_ref');
      if (document.getElementById('qtype-cloze').checked) qtypes.push('cloze');
      if (document.getElementById('qtype-mc').checked) qtypes.push('multiple_choice');
      if (document.getElementById('qtype-continue').checked) qtypes.push('continue_verse');
      if (document.getElementById('qtype-mc-text').checked) qtypes.push('multiple_choice_text');
      if (qtypes.length===0){ alert('최소한 하나의 문제 유형을 선택하세요.'); return; }

      // ★ 시험은 순수 랜덤(균등) + 비복원 + 참조 유니크
      const pickedIdx = pickUniqueIndicesUniform(num);

      const questions = [];
      for (let i=0;i<pickedIdx.length;i++){
        const v = VERSES[pickedIdx[i]];
        const t = qtypes[randInt(qtypes.length)];
        questions.push(makeQuestion(t, v));
      }

      PRACTICE_MODE = false;
      updatePracticeToggleUI(false);
      setScoreVisibility(false); // 시험 중에는 점수/스킵 숨김

      CURRENT_QUIZ = { questions, index: 0, score: 0, skip: 0, answered: new Array(questions.length).fill(false), practice:false, originSessionId:null };
      LAST_RESULT = null;

      document.getElementById('mode-badge').style.display = 'none';
      document.getElementById('practice-acc').style.display = 'none';
      document.getElementById('quiz-panel').classList.remove('hidden');
      document.getElementById('result-panel').classList.add('hidden');
      document.getElementById('total-count').textContent = questions.length;
      document.getElementById('cur-score').textContent = '0';
      document.getElementById('cur-skip').textContent = '0';
      renderQuestion();
      showView('quiz');
    }

    async function startTop20Quiz(){
      if (!CURRENT_USER){ alert('시험을 시작하려면 로그인하세요.'); showView('auth'); return; }
      if (VERSES.length === 0){
        await loadDefaultVersesFromServer(true);
        if (VERSES.length === 0){ alert('서버 verses.csv를 찾을 수 없습니다. 설정에서 CSV를 업로드하세요.'); showView('settings'); return; }
      }

      const d = await apiGet('/data');
      const verseScores = d.verseScores || {};

      // verseScores 상위 20(>0)만 가져오기
      const tops = Object.entries(verseScores)
        .filter(([,v]) => (v||0) > 0)
        .sort((a,b) => b[1]-a[1])
        .slice(0, 20);

      if (tops.length === 0){
        alert('오답 TOP20 항목이 없습니다. 먼저 문제를 풀어 데이터가 쌓여야 합니다.');
        return;
      }

      // 메모리의 VERSES에서 존재하는 것만 골라 준비
      const verseMap = new Map(VERSES.map(v => [verseKey(v), v]));
      const selectedVerses = [];
      const used = new Set();
      for (const [key] of tops){
        if (used.has(key)) continue;
        const v = verseMap.get(key);
        if (v) { selectedVerses.push(v); used.add(key); }
      }
      if (selectedVerses.length === 0){
        alert('현재 로드된 CSV에 TOP20 참조가 존재하지 않습니다. CSV를 확인해주세요.');
        return;
      }

      const qtypes = [];
      if (document.getElementById('qtype-identify').checked) qtypes.push('identify_ref');
      if (document.getElementById('qtype-cloze').checked) qtypes.push('cloze');
      if (document.getElementById('qtype-mc').checked) qtypes.push('multiple_choice');
      if (document.getElementById('qtype-continue').checked) qtypes.push('continue_verse');
      if (document.getElementById('qtype-mc-text').checked) qtypes.push('multiple_choice_text');
      if (qtypes.length===0){ alert('최소한 하나의 문제 유형을 선택하세요.'); return; }

      // 선택된 구절들로 세트 구성(유형은 랜덤 배정)
      const questions = selectedVerses.map(v => makeQuestion(qtypes[randInt(qtypes.length)], v));

      PRACTICE_MODE = false;
      updatePracticeToggleUI(false);
      setScoreVisibility(false); // 시험 중에는 점수/스킵 숨김

      CURRENT_QUIZ = { questions, index: 0, score: 0, skip: 0, answered: new Array(questions.length).fill(false), practice:false, originSessionId:null };
      LAST_RESULT = null;

      document.getElementById('mode-badge').style.display = 'none';
      document.getElementById('practice-acc').style.display = 'none';
      document.getElementById('quiz-panel').classList.remove('hidden');
      document.getElementById('result-panel').classList.add('hidden');
      document.getElementById('total-count').textContent = questions.length;
      document.getElementById('cur-score').textContent = '0';
      document.getElementById('cur-skip').textContent = '0';
      renderQuestion();
      showView('quiz');
    }

    async function retakeSession(sessionId){
      if (!CURRENT_USER){ alert('재시험을 시작하려면 로그인하세요.'); showView('auth'); return; }
      const d = await apiGet('/data');
      const se = (d.sessions||[]).find(x=> x.id===sessionId);
      if (!se){ alert('저장된 세트를 찾을 수 없습니다.'); return; }

      if (!se.questionsDump){
        alert('이 세트에는 문제 덤프가 없어 재시험을 진행할 수 없습니다.');
        return;
      }
      const questions = se.questionsDump.map(q=>({ ...q, verse: q.verse }));

      PRACTICE_MODE = false;
      updatePracticeToggleUI(false);
      setScoreVisibility(false); // 시험 중에는 점수/스킵 숨김

      CURRENT_QUIZ = { questions, index:0, score:0, skip:0, answered:new Array(questions.length).fill(false), practice:false, originSessionId: sessionId };
      LAST_RESULT = null;

      document.getElementById('mode-badge').style.display = 'none';
      document.getElementById('practice-acc').style.display = 'none';
      document.getElementById('quiz-panel').classList.remove('hidden');
      document.getElementById('result-panel').classList.add('hidden');
      document.getElementById('total-count').textContent = questions.length;
      document.getElementById('cur-score').textContent = '0';
      document.getElementById('cur-skip').textContent = '0';
      renderQuestion();
      showView('quiz');
    }

    // ★ 틀린 문제만 다시 풀기
    async function retakeWrongOnly(sessionId){
      if (!CURRENT_USER){ alert('재시험을 시작하려면 로그인하세요.'); showView('auth'); return; }
      const d = await apiGet('/data');
      const se = (d.sessions||[]).find(x=> x.id===sessionId);
      if (!se){ alert('저장된 세트를 찾을 수 없습니다.'); return; }
      if (!se.questionsDump){
        alert('이 세트에는 문제 덤프가 없어 틀린 문제만 재시험을 진행할 수 없습니다.');
        return;
      }
      const qs = se.questionsDump || [];
      const details = se.details || [];

      const usedRef = new Set();
      const wrongQuestions = [];
      for (let i=0;i<qs.length;i++){
        const drec = details[i] || {};
        const isWrong = drec.skipped || !drec.correct;
        if (!isWrong) continue;
        const v = qs[i].verse;
        const key = `${v.book}|${v.chapter}|${v.verse}`;
        if (usedRef.has(key)) continue; // 참조 중복 방지
        usedRef.add(key);
        wrongQuestions.push({ ...qs[i], verse: qs[i].verse });
      }

      if (wrongQuestions.length===0){
        alert('틀린 문제가 없습니다. 잘하셨어요!');
        return;
      }

      PRACTICE_MODE = false;
      updatePracticeToggleUI(false);
      setScoreVisibility(false); // 시험 중에는 점수/스킵 숨김

      CURRENT_QUIZ = { questions: wrongQuestions, index:0, score:0, skip:0, answered:new Array(wrongQuestions.length).fill(false), practice:false, originSessionId: null };
      LAST_RESULT = null;

      document.getElementById('mode-badge').style.display = 'none';
      document.getElementById('practice-acc').style.display = 'none';
      document.getElementById('quiz-panel').classList.remove('hidden');
      document.getElementById('result-panel').classList.add('hidden');
      document.getElementById('total-count').textContent = wrongQuestions.length;
      document.getElementById('cur-score').textContent = '0';
      document.getElementById('cur-skip').textContent = '0';
      renderQuestion();
      showView('quiz');
    }

    // ------------------------------
    // 학습(무한) 모드
    // ------------------------------
    function updatePracticeToggleUI(isOn){
      const btn = document.getElementById('btn-practice-toggle');
      if (isOn){
        btn.classList.remove('btn-success'); btn.classList.add('btn-danger');
        btn.textContent = '학습하기 중지';
        document.getElementById('mode-badge').style.display = '';
        document.getElementById('practice-acc').style.display = '';
        document.getElementById('total-count').textContent = '∞';
        setScoreVisibility(true); // 학습 중에는 점수/스킵 표시
      } else {
        btn.classList.remove('btn-danger'); btn.classList.add('btn-success');
        btn.textContent = '학습하기 시작';
        document.getElementById('mode-badge').style.display = 'none';
        document.getElementById('practice-acc').style.display = 'none';
        setScoreVisibility(false); // 학습 중지 시 숨김(시험 대비)
      }
    }

    async function togglePractice(){
      if (!CURRENT_USER){ alert('학습하려면 로그인하세요.'); showView('auth'); return; }
      if (VERSES.length === 0){
        await loadDefaultVersesFromServer(true);
        if (VERSES.length === 0){ alert('서버 verses.csv를 찾을 수 없습니다. 설정에서 CSV를 업로드하세요.'); showView('settings'); return; }
      }
      PRACTICE_MODE = !PRACTICE_MODE;
      if (PRACTICE_MODE){
        const d = await apiGet('/data');
        const qtypes = [];
        if (document.getElementById('qtype-identify').checked) qtypes.push('identify_ref');
        if (document.getElementById('qtype-cloze').checked) qtypes.push('cloze');
        if (document.getElementById('qtype-mc').checked) qtypes.push('multiple_choice');
        if (document.getElementById('qtype-continue').checked) qtypes.push('continue_verse');
        if (document.getElementById('qtype-mc-text').checked) qtypes.push('multiple_choice_text');
        if (qtypes.length===0){ alert('최소한 하나의 문제 유형을 선택하세요.'); PRACTICE_MODE=false; return; }

        // 학습 모드 선택기 초기화 (랜덤↔가중치 번갈아 + 재출제큐)
        const sel = initPracticeSelector(d, qtypes);

        const firstQ = sel.makeOne(); // CURRENT_QUIZ 생성 전에 호출해도 안전

        CURRENT_QUIZ = {
          questions:[firstQ], index:0, score:0, skip:0, answered:[false],
          practice:true, makeOne: sel.makeOne, practiceStats:{attempts:0, correct:0},
          practiceRetryQueue: sel.retryQueue,
          practiceRecent: sel.recentKeys,
          scheduleRetryForVerse: sel.scheduleRetryForVerse,
          discardFromRetryByKey: sel.discardFromRetryByKey,
          onAttempt: sel.onAttempt
        };
        LAST_RESULT = null;

        updatePracticeToggleUI(true);
        document.getElementById('quiz-panel').classList.remove('hidden');
        document.getElementById('result-panel').classList.add('hidden');
        renderQuestion();
        showView('quiz');
        updatePracticeAccuracyLabel();
      } else {
        updatePracticeToggleUI(false);
      }
    }

    function updatePracticeAccuracyLabel(){
      if (!CURRENT_QUIZ || !CURRENT_QUIZ.practice || !CURRENT_QUIZ.practiceStats) return;
      const a = CURRENT_QUIZ.practiceStats.attempts || 0;
      const c = CURRENT_QUIZ.practiceStats.correct || 0;
      const pct = a ? Math.round(c/a*100) : 0;
      const el = document.getElementById('practice-acc');
      el.textContent = `정확도: ${pct}% (${c}/${a})`;
    }

    // ------------------------------
    // 렌더링
    // ------------------------------
    function renderQuestion(){
      const q = CURRENT_QUIZ.questions[CURRENT_QUIZ.index];
      const box = document.getElementById('question-content');
      const btnPrev = document.getElementById('btn-prev');
      const btnNext = document.getElementById('btn-next');
      const btnSubmit = document.getElementById('btn-submit');

      if (CURRENT_QUIZ.practice){
        btnPrev.disabled = true;
        btnNext.textContent = '다음 문제';
        btnNext.disabled = !CURRENT_QUIZ.answered[CURRENT_QUIZ.index];
      } else {
        btnPrev.disabled = CURRENT_QUIZ.index===0;
        btnNext.textContent = '다음';
        btnNext.disabled = CURRENT_QUIZ.index>=CURRENT_QUIZ.questions.length-1;
      }
      btnSubmit.disabled = CURRENT_QUIZ.answered[CURRENT_QUIZ.index];

      document.getElementById('cur-index').textContent = CURRENT_QUIZ.index+1;

      const headerType = `<div class="text-muted small">유형: <span class="mono">${q.qtype}</span></div>`;
      const headerRef = `<div class="text-muted small">참조: <span class="mono">${formatRef(q.verse)}</span></div>`;
      const showRefInQuestion = (q.qtype==='cloze' || q.qtype==='continue_verse' || q.qtype==='multiple_choice_text');

      if (q.qtype==='identify_ref'){
        box.innerHTML = `
          <div class="mb-2">${q.prompt}</div>
          ${headerType}
          <blockquote class="border-start border-3 ps-3">${q.verse.text}</blockquote>
          <div class="row g-2 mt-2">
            <div class="col-12 col-md-4">
              <label class="form-label">책</label>
              <input class="form-control" id="ans-book" placeholder="예: 요한복음" />
            </div>
            <div class="col-6 col-md-4">
              <label class="form-label">장</label>
              <input type="number" class="form-control" id="ans-chapter" />
            </div>
            <div class="col-6 col-md-4">
              <label class="form-label">절</label>
              <input type="number" class="form-control" id="ans-verse" />
            </div>
          </div>`;
      } else if (q.qtype==='cloze'){
        box.innerHTML = `
          <div class="mb-2">${q.prompt}</div>
          ${headerType} ${showRefInQuestion?headerRef:''}
          <blockquote class="border-start border-3 ps-3">${q.maskedHtml}</blockquote>
          <label class="form-label mt-2">복원한 문장(원문과 최대한 일치):</label>
          <textarea class="form-control" id="ans-cloze" rows="2" placeholder="띄어쓰기 무시: 정확히 쓰면 더 유리합니다"></textarea>`;
      } else if (q.qtype==='continue_verse'){
        box.innerHTML = `
          <div class="mb-2">${q.prompt}</div>
          ${headerType} ${showRefInQuestion?headerRef:''}
          <label class="form-label">구절 전체:</label>
          <textarea class="form-control" id="ans-continue" rows="3" placeholder="띄어쓰기 무시: 원문과 최대한 일치하게 입력"></textarea>`;
      } else if (q.qtype==='multiple_choice_text'){
        const opts = q.options.map((o,idx)=>{
          return `<div class="form-check"><input class="form-check-input" type="radio" name="mc2" id="mc2_${idx}" value="${idx}"><label class="form-check-label" for="mc2_${idx}">${o}</label></div>`
        }).join('');
        box.innerHTML = `
          <div class="mb-2">${q.prompt}</div>
          ${headerType} ${showRefInQuestion?headerRef:''}
          <div class="mt-2">${opts}</div>`;
      } else {
        const opts = q.options.map((o,idx)=>{
          return `<div class="form-check"><input class="form-check-input" type="radio" name="mc" id="mc${idx}" value="${idx}"><label class="form-check-label" for="mc${idx}">${o}</label></div>`
        }).join('');
        box.innerHTML = `
          <div class="mb-2">${q.prompt}</div>
          ${headerType}
          <blockquote class="border-start border-3 ps-3">${q.verse.text}</blockquote>
          <div class="mt-2">${opts}</div>`;
      }
    }

    // ------------------------------
    // 채점 + 기록
    // ------------------------------
    function submitAnswer(){
      const i = CURRENT_QUIZ.index;
      if (CURRENT_QUIZ.answered[i]) return;
      const q = CURRENT_QUIZ.questions[i];
      let correct = false; let userAnswerDisplay = '';

      if (q.qtype==='identify_ref'){
        const b = document.getElementById('ans-book').value;
        const c = parseInt(document.getElementById('ans-chapter').value,10);
        const v = parseInt(document.getElementById('ans-verse').value,10);
        userAnswerDisplay = `${b||'-'} ${Number.isFinite(c)?c:'-'}:${Number.isFinite(v)?v:'-'}`;
        const bookOK = normalizeLabel(b)===normalizeLabel(q.verse.book);
        correct = bookOK && c===q.verse.chapter && v===q.verse.verse;
      } else if (q.qtype==='cloze'){
        const t = document.getElementById('ans-cloze').value;
        userAnswerDisplay = t;
        const ratio = charSimIgnoreSpaces(q.verse.text, t); // 띄어쓰기 무시
        correct = ratio >= 0.70;
      } else if (q.qtype==='continue_verse'){
        const t = document.getElementById('ans-continue').value;
        userAnswerDisplay = t;
        const ratio = charSimIgnoreSpaces(q.verse.text, t); // 띄어쓰기 무시
        correct = ratio >= 0.85;
      } else if (q.qtype==='multiple_choice_text'){
        const sel = document.querySelector('input[name="mc2"]:checked');
        if (!sel){ alert('보기를 선택하세요.'); return; }
        const ans = parseInt(sel.value,10);
        userAnswerDisplay = q.options[ans];
        correct = (ans === q.correctIndex);
      } else {
        const sel = document.querySelector('input[name="mc"]:checked');
        if (!sel){ alert('보기를 선택하세요.'); return; }
        const ans = parseInt(sel.value,10);
        userAnswerDisplay = q.options[ans];
        correct = (ans === q.correctIndex);
      }

      q.userAnswerDisplay = userAnswerDisplay;
      q.correct = !!correct;
      q.skipped = false;
      CURRENT_QUIZ.answered[i] = true;
      if (correct) CURRENT_QUIZ.score++;
      document.getElementById('cur-score').textContent = CURRENT_QUIZ.score;

      if (CURRENT_QUIZ.practice){
        // 오답/스킵은 재출제 큐에 스케줄
        if (!q.correct && typeof CURRENT_QUIZ.scheduleRetryForVerse === 'function'){
          const attemptsSoFar = (CURRENT_QUIZ.practiceStats?.attempts||0);
          CURRENT_QUIZ.scheduleRetryForVerse(q.verse, attemptsSoFar);
        } else if (q.correct && typeof CURRENT_QUIZ.discardFromRetryByKey === 'function'){
          CURRENT_QUIZ.discardFromRetryByKey(verseKey(q.verse));
        }

        revealImmediateAnswer(q);
        document.getElementById('btn-next').disabled = false;
        document.getElementById('btn-submit').disabled = true;
        // 누적 정확도 업데이트
        if (!CURRENT_QUIZ.practiceStats) CURRENT_QUIZ.practiceStats = {attempts:0, correct:0};
        CURRENT_QUIZ.practiceStats.attempts++;
        if (typeof CURRENT_QUIZ.onAttempt === 'function') CURRENT_QUIZ.onAttempt(); // ← selector 시도수 증가
        if (q.correct) CURRENT_QUIZ.practiceStats.correct++;
        updatePracticeAccuracyLabel();
        // 학습 모드: 개별 문항 자동 저장 (사용자별)
        autoLogPracticeAttempt(q);
      } else {
        if (i < CURRENT_QUIZ.questions.length-1){ CURRENT_QUIZ.index++; renderQuestion(); }
        else { showResult(true); } // 시험 종료 자동 저장
      }
    }

    function skipQuestion(){
      const i = CURRENT_QUIZ.index;
      if (CURRENT_QUIZ.answered[i]) return;
      const q = CURRENT_QUIZ.questions[i];
      q.skipped = true; q.correct = false; q.userAnswerDisplay = '';
      CURRENT_QUIZ.answered[i] = true;
      CURRENT_QUIZ.skip++;
      document.getElementById('cur-skip').textContent = CURRENT_QUIZ.skip;

      if (CURRENT_QUIZ.practice){
        // 스킵도 재출제 큐에 스케줄
        if (typeof CURRENT_QUIZ.scheduleRetryForVerse === 'function'){
          const attemptsSoFar = (CURRENT_QUIZ.practiceStats?.attempts||0);
          CURRENT_QUIZ.scheduleRetryForVerse(q.verse, attemptsSoFar);
        }

        revealImmediateAnswer(q, true);
        document.getElementById('btn-next').disabled = false;
        document.getElementById('btn-submit').disabled = true;
        if (!CURRENT_QUIZ.practiceStats) CURRENT_QUIZ.practiceStats = {attempts:0, correct:0};
        CURRENT_QUIZ.practiceStats.attempts++;
        if (typeof CURRENT_QUIZ.onAttempt === 'function') CURRENT_QUIZ.onAttempt(); // ← selector 시도수 증가
        updatePracticeAccuracyLabel();
        autoLogPracticeAttempt(q);
      } else {
        if (i < CURRENT_QUIZ.questions.length-1){ CURRENT_QUIZ.index++; renderQuestion(); }
        else { showResult(true); }
      }
    }

    function autoLogPracticeAttempt(q){
      // 로그인 필요
      if (!CURRENT_USER) return;
      const session = {
        id: 'sess_'+Date.now()+'_'+Math.floor(Math.random()*1e6),
        type: 'practice',
        dateISO: new Date().toISOString(),
        total: 1,
        correct: q.correct ? 1 : 0,
        skip: q.skipped ? 1 : 0,
        score: q.correct ? 1 : 0,
        details: [{
          qtype: q.qtype,
          subj: q.qtype!=='multiple_choice' && q.qtype!=='multiple_choice_text',
          book: q.verse.book, chapter: q.verse.chapter, verse: q.verse.verse,
          text: q.verse.text,
          correct: !!q.correct,
          skipped: !!q.skipped
        }]
      };
      apiPost('/save', { session }).then(()=>{ buildDashboard(); });
    }

    function revealImmediateAnswer(q, wasSkipped=false){
      const box = document.getElementById('question-content');
      let correctAnswer;
      if (q.qtype==='multiple_choice') correctAnswer = formatRef(q.verse);
      else if (q.qtype==='multiple_choice_text') correctAnswer = `“${q.verse.text}”`;
      else if (q.qtype==='identify_ref') correctAnswer = formatRef(q.verse);
      else if (q.qtype==='cloze' || q.qtype==='continue_verse') correctAnswer = q.verse.text;
      else correctAnswer = formatRef(q.verse);

      const status = wasSkipped ? '스킵' : (q.correct ? '정답' : '오답');
      const html = `
        <div class="answer-reveal">
          <div><strong>채점:</strong> ${status}</div>
          <div class="mt-1"><strong>정답:</strong> ${correctAnswer}</div>
          <div class="mt-1"><strong>참조:</strong> <span class="mono">${formatRef(q.verse)}</span></div>
          <div class="mt-1"><strong>문구:</strong> “${q.verse.text}”</div>
          <div class="mt-1"><strong>내 답:</strong> ${wasSkipped ? '(스킵)' : (q.userAnswerDisplay||'-')}</div>
        </div>`;
      box.insertAdjacentHTML('beforeend', html);
    }

    function prevQuestion(){ if (!CURRENT_QUIZ || CURRENT_QUIZ.practice) return; if (CURRENT_QUIZ.index>0){ CURRENT_QUIZ.index--; renderQuestion(); } }
    function nextQuestion(){
      if (!CURRENT_QUIZ) return;
      if (CURRENT_QUIZ.practice){
        if (!CURRENT_QUIZ.answered[CURRENT_QUIZ.index]) return;
        const nextQ = CURRENT_QUIZ.makeOne();
        CURRENT_QUIZ.questions = [nextQ];
        CURRENT_QUIZ.answered = [false];
        CURRENT_QUIZ.index = 0;
        renderQuestion();
        return;
      }
      if (CURRENT_QUIZ.index<CURRENT_QUIZ.questions.length-1){
        CURRENT_QUIZ.index++; renderQuestion();
      }
    }

    // ------------------------------
    // 결과/요약 및 저장 (저장 완료 보장)
    // ------------------------------
    async function showResult(autoSave=false){
      const total = CURRENT_QUIZ.questions.length;
      const correct = CURRENT_QUIZ.score;
      const skip = CURRENT_QUIZ.skip;
      document.getElementById('res-total').textContent = total;
      document.getElementById('res-correct').textContent = correct;
      document.getElementById('res-skip').textContent = skip;
      document.getElementById('res-score').textContent = `${correct}/${total}`;

      const list = document.getElementById('result-list');
      list.innerHTML = '';
      CURRENT_QUIZ.questions.forEach((q,idx)=>{
        const typeLabel = (q.qtype==='multiple_choice' || q.qtype==='multiple_choice_text') ? '객관식' : '주관식';
        const badge = q.skipped ? '<span class="badge text-bg-warning">스킵</span>' : (q.correct ? '<span class="badge text-bg-success">정답</span>' : '<span class="badge text-bg-danger">오답</span>');
        let correctAnswer;
        if (q.qtype==='multiple_choice') correctAnswer = formatRef(q.verse);
        else if (q.qtype==='multiple_choice_text') correctAnswer = `“${q.verse.text}”`;
        else if (q.qtype==='identify_ref') correctAnswer = formatRef(q.verse);
        else if (q.qtype==='cloze' || q.qtype==='continue_verse') correctAnswer = q.verse.text;
        else correctAnswer = formatRef(q.verse);

        const card = document.createElement('div');
        card.className = 'col-12';
        card.innerHTML = `
          <div class="card question-card ${q.correct?'correct':(q.skipped?'':'incorrect')}">
            <div class="card-body">
              <div class="d-flex justify-content-between align-items-start">
                <h6 class="mb-2">Q${idx+1} <small class="text-muted">(${typeLabel} / ${q.qtype})</small></h6>
                ${badge}
              </div>
              <div class="mb-2"><strong>참조:</strong> <span class="mono">${formatRef(q.verse)}</span></div>
              <div class="mb-2"><strong>문구:</strong> “${q.verse.text}”</div>
              <div class="mb-2"><strong>문제:</strong> ${q.prompt}</div>
              <div><strong>정답:</strong> ${correctAnswer}</div>
              <div class="mt-1"><strong>내 답:</strong> ${q.skipped ? '(스킵)' : (q.userAnswerDisplay||'-')}</div>
            </div>
          </div>`;
        list.appendChild(card);
      });

      LAST_RESULT = summarizeCurrent(true); // 세트 저장 포함
      LAST_RESULT.type = 'exam';

      // 재시험이면 기존 세트 id로 덮어쓰기
      let replaceId = null;
      if (CURRENT_QUIZ.originSessionId){
        LAST_RESULT.id = CURRENT_QUIZ.originSessionId;
        replaceId = CURRENT_QUIZ.originSessionId;
      }

      document.getElementById('quiz-panel').classList.add('hidden');
      document.getElementById('result-panel').classList.remove('hidden');

      if (autoSave && LAST_RESULT){
        const payload = replaceId ? { session: LAST_RESULT, replaceId } : { session: LAST_RESULT };
        const res = await apiPost('/save', payload);
        if (!(res && res.ok)) { alert(res?.error || '저장 중 오류가 발생했습니다.'); }
        await buildDashboard(); // 저장 완료 후 대시보드 동기화
      }
    }

    function summarizeCurrent(includeDump=false){
      const details = CURRENT_QUIZ.questions.map(q=>({
        qtype: q.qtype,
        subj: q.qtype!=='multiple_choice' && q.qtype!=='multiple_choice_text',
        book: q.verse.book, chapter: q.verse.chapter, verse: q.verse.verse,
        text: q.verse.text,
        correct: !!q.correct,
        skipped: !!q.skipped
      }));
      const uniq = Date.now()+'_'+Math.floor(Math.random()*1e6);
      const base = {
        id: 'sess_'+uniq,
        dateISO: new Date().toISOString(),
        total: CURRENT_QUIZ.questions.length,
        correct: CURRENT_QUIZ.score,
        skip: CURRENT_QUIZ.skip,
        score: CURRENT_QUIZ.score,
        originSessionId: CURRENT_QUIZ.originSessionId || null,
        details
      };
      if (includeDump){
        // 재시험을 위한 세트 덤프(문항 그대로)
        base["questionsDump"] = CURRENT_QUIZ.questions.map(q=>({
          qtype: q.qtype,
          subj: q.subj,
          verse: {book:q.verse.book, chapter:q.verse.chapter, verse:q.verse.verse, text:q.verse.text},
          prompt: q.prompt,
          options: q.options || null,
          correctIndex: (typeof q.correctIndex==='number') ? q.correctIndex : null,
          maskedHtml: q.maskedHtml || null
        }));
      }
      return base;
    }

    async function clearAll(){
      if (!CURRENT_USER){ alert('기록을 초기화하려면 로그인하세요.'); showView('auth'); return; }
      if (!confirm('서버에 저장된 (내) 모든 기록과 설정을 초기화할까요?')) return;
      const res = await apiPost('/reset', {});
      if (res && res.ok){
        alert('초기화되었습니다.');
        buildDashboard();
        refreshMeta();
        await loadSettings(); // 기본 설정으로 반영
      }
    }

    // ------------------------------
    // 대시보드 구축 (+ 과거 시험/재시험/틀린만 재시험, TOP20, 랭킹, 최근 오답율)
    // ------------------------------
    async function buildDashboard(){
      const d = await apiGet('/data');
      if (!d) return;
      const sessions = (d.sessions||[]).slice();

      // 1) 과거 시험 표 (시험 성격 세트만 표시)
      try {
        const tb = document.getElementById('table-past-sessions');
        if (tb){ tb.innerHTML = ''; }
        const examSessions = sessions.filter(se=> se?.type==='exam' || (se?.total||0) > 1);
        examSessions.forEach((se,idx)=>{
          const dt = new Date(se.dateISO || Date.now());
          const name = `세트 ${idx+1}`;
          const accuracy = se.total ? Math.round((se.correct||0)/se.total*100) : 0;
          const canRetake = !!se.questionsDump;
          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td class="nowrap">${name}</td>
            <td class="nowrap">${dt.toLocaleString()}</td>
            <td class="text-end">${se.total||0}</td>
            <td class="text-end">${se.correct||0}</td>
            <td class="text-end">${accuracy}%</td>
            <td>
              <div class="btn-group btn-group-sm">
                <button class="btn btn-outline-primary" data-sid="${se.id}" ${canRetake?'':'disabled'}>재시험</button>
                <button class="btn btn-outline-danger" data-sid="${se.id}" data-retake-wrong="1" ${canRetake?'':'disabled'}>틀린문제만</button>
                <button class="btn btn-outline-secondary" data-sid="${se.id}" data-review="1">결과보기</button>
                <button class="btn btn-outline-dark" data-sid="${se.id}" data-delete="1">삭제</button>
              </div>
            </td>`;
          if (tb) tb.appendChild(tr);
        });

        // 버튼 바인딩
        if (tb){
          tb.querySelectorAll('button').forEach(btn=>{
            btn.addEventListener('click', async (ev)=>{
              const sid = ev.target.getAttribute('data-sid');
              const isReview = ev.target.getAttribute('data-review');
              const isWrongOnly = ev.target.getAttribute('data-retake-wrong');
              const isDelete = ev.target.getAttribute('data-delete');
              if (isDelete){
                if (!confirm('이 시험 세트를 삭제할까요?')) return;
                const res = await apiPost('/delete_session', {id: sid});
                if (res && res.ok){ buildDashboard(); }
                return;
              }

              const d2 = await apiGet('/data');
              const se = (d2.sessions||[]).find(x=> x.id===sid);
              if (!se){ alert('결과 데이터를 찾을 수 없습니다.'); return; }

              if (isReview){
                // 과거 결과 보기
                const list = document.getElementById('result-list');
                list.innerHTML = '';
                const qs = se.questionsDump || [];
                const details = se.details || [];
                if (qs.length === 0){
                  const msg = document.createElement('div');
                  msg.className = 'alert alert-warning';
                  msg.textContent = '이 세트에는 문제 덤프가 없어 상세 결과를 표시할 수 없습니다.';
                  list.appendChild(msg);
                } else {
                  qs.forEach((q,idx)=>{
                    const verse = q.verse;
                    const typeLabel = (q.qtype==='multiple_choice' || q.qtype==='multiple_choice_text') ? '객관식' : '주관식';

                    const drec = details[idx] || {}
                    const badge = drec.skipped ? '<span class="badge text-bg-warning">스킵</span>' : (drec.correct ? '<span class="badge text-bg-success">정답</span>' : '<span class="badge text-bg-danger">오답</span>');

                    let correctAnswer;
                    if (q.qtype==='multiple_choice') correctAnswer = `${verse.book} ${verse.chapter},${verse.verse}`;
                    else if (q.qtype==='multiple_choice_text') correctAnswer = `“${verse.text}”`;
                    else if (q.qtype==='identify_ref') correctAnswer = `${verse.book} ${verse.chapter},${verse.verse}`;
                    else if (q.qtype==='cloze' || q.qtype==='continue_verse') correctAnswer = verse.text;
                    else correctAnswer = `${verse.book} ${verse.chapter},${verse.verse}`;

                    const card = document.createElement('div');
                    card.className = 'col-12';
                    card.innerHTML = `
                      <div class="card question-card ${drec.correct?'correct':(drec.skipped?'':'incorrect')}">
                        <div class="card-body">
                          <div class="d-flex justify-content-between align-items-start">
                            <h6 class="mb-2">Q${idx+1} <small class="text-muted">(${typeLabel} / ${q.qtype})</small></h6>
                            ${badge}
                          </div>
                          <div class="mb-2"><strong>참조:</strong> <span class="mono">${verse.book} ${verse.chapter},${verse.verse}</span></div>
                          <div class="mb-2"><strong>문구:</strong> “${verse.text}”</div>
                          <div class="mb-2"><strong>문제:</strong> ${q.prompt}</div>
                          <div><strong>정답:</strong> ${correctAnswer}</div>
                        </div>
                      </div>`;
                    list.appendChild(card);
                  });
                }
                document.getElementById('res-total').textContent = se.total||0;
                document.getElementById('res-correct').textContent = se.correct||0;
                document.getElementById('res-skip').textContent = se.skip||0;
                document.getElementById('res-score').textContent = `${se.correct||0}/${se.total||0}`;
                document.getElementById('quiz-panel').classList.add('hidden');
                document.getElementById('result-panel').classList.remove('hidden');
                showView('quiz');
              } else if (isWrongOnly){
                retakeWrongOnly(sid);
              } else {
                retakeSession(sid);
              }
            });
          });
        }
      } catch(e){ console.warn('past-sessions build error', e); }

      // 2) 요약/차트: 시험 세션만 반영 (practice 제외)
      try {
        const exams = sessions.filter(se => se.type === 'exam' || (se?.total||0) > 1);
        document.getElementById('stat-total-tests').textContent = exams.length;
        document.getElementById('stat-last-score').textContent = exams.length ? `${exams[exams.length-1].correct||0}/${exams[exams.length-1].total||0}` : '-';
        const avg = exams.length ? (exams.reduce((acc,x)=>acc + (x.correct||0)/(x.total||1)*30,0)/exams.length) : 0;
        document.getElementById('stat-avg-score').textContent = exams.length ? avg.toFixed(1)+'/30' : '-';

        let subjC=0, subjT=0, objC=0, objT=0, skipC=0, skipT=0;
        for (const se of exams){
          for (const d0 of (se.details||[])){
            const d = d0||{};
            if (d.subj){subjT++; if (d.correct) subjC++;} else {objT++; if (d.correct) objC++;}
            skipT++; if (d.skipped) skipC++;
          }
        }
        document.getElementById('stat-subj-acc').textContent = subjT? ((subjC/subjT*100).toFixed(1)+'%') : '-';
        document.getElementById('stat-obj-acc').textContent = objT? ((objC/objT*100).toFixed(1)+'%') : '-';
        document.getElementById('stat-skip-rate').textContent = skipT? ((skipC/skipT*100).toFixed(1)+'%') : '-';

        const labels = exams.map((_,i)=> i+1);
        const scores = exams.map(se=> Math.round((se.correct||0)/(se.total||1)*30));
        const ctx = document.getElementById('chart-scores').getContext('2d');
        if (chartScores) { chartScores.destroy(); }
        chartScores = new Chart(ctx, {
          type: 'line',
          data: { labels, datasets: [{ label: '점수(30점 만점 환산)', data: scores }] },
          options: { responsive: true, scales: { y: { beginAtZero: true, suggestedMax: 30 } } }
        });

        // 3) 오답 TOP20: verseScores 기반
        const verseScores = d.verseScores || {};
        const entries = Object.entries(verseScores).filter(([,v])=> (v||0)>0).sort((a,b)=> (b[1] - a[1])).slice(0,20);

        const textIndex = new Map();
        for (const v of VERSES){
          textIndex.set(`${v.book}|${v.chapter}|${v.verse}`, v.text);
        }

        const ul = document.getElementById('wrong-top'); if (ul) ul.innerHTML = '';
        for (const [key, cnt] of entries){
          const [book, chapter, verse] = key.split('|');
          const numC = parseInt(chapter,10), numV = parseInt(verse,10);
          const verseText = textIndex.get(key) || '(텍스트 로드 전 또는 미상)';
          const li = document.createElement('li');
          li.className = 'list-group-item d-flex justify-content-between align-items-center';
          li.innerHTML = `
            <span>[${book} ${numC},${numV}] ${verseText}</span>
            <span class="d-flex align-items-center gap-2">
              <span class="badge bg-danger">${cnt}</span>
              <button class="btn btn-sm btn-outline-dark" data-del-key="${key}">삭제</button>
            </span>`;
          ul?.appendChild(li);
        }
        // TOP20 삭제 버튼 바인딩
        if (ul){
          ul.querySelectorAll('button[data-del-key]').forEach(btn=>{
            btn.addEventListener('click', async (ev)=>{
              const key = ev.target.getAttribute('data-del-key');
              if (!confirm('이 항목을 TOP20에서 제거(카운트 리셋)할까요?')) return;
              const res = await apiPost('/delete_verse_score', {key});
              if (res && res.ok){ buildDashboard(); }
            });
          });
        }

        // 4) 책별 최근 오답율 (최근 100문항, 시험+학습 모두 포함)
        try {
          const recentWindow = 100;
          const flattened = []; // [{book, correct, skipped, dateISO}]
          // 세션을 최신순으로 훑으면서 details를 최신순으로 채우기
          const sorted = sessions.slice().sort((a,b)=> new Date(b.dateISO||0) - new Date(a.dateISO||0));
          for (const se of sorted){
            const det = (se.details||[]).slice(); // 원본 보존
            // 세트 내부 문항도 뒤에서부터(최신) 넣자
            for (let i=det.length-1; i>=0; i--){
              if (flattened.length >= recentWindow) break;
              const d0 = det[i];
              flattened.push({ book: d0.book, correct: !!d0.correct, skipped: !!d0.skipped, dateISO: se.dateISO });
            }
            if (flattened.length >= recentWindow) break;
          }
          const byBook = new Map();
          for (const r of flattened){
            const b = r.book || '(미상)';
            if (!byBook.has(b)) byBook.set(b, {t:0,w:0});
            byBook.get(b).t++;
            if (r.skipped || !r.correct) byBook.get(b).w++;
          }
          const rows = Array.from(byBook.entries()).map(([book,st])=>{
            const rate = st.t? (st.w/st.t*100) : 0;
            return {book, t:st.t, w:st.w, rate};
          }).sort((a,b)=> b.rate - a.rate || b.t - a.t || a.book.localeCompare(b.book));
          const tbody = document.getElementById('table-recent-wrong-by-book');
          if (tbody){
            tbody.innerHTML = '';
            for (const r of rows){
              const tr = document.createElement('tr');
              tr.innerHTML = `<td>${r.book}</td><td class="text-end">${r.t}</td><td class="text-end">${r.w}</td><td class="text-end">${r.rate.toFixed(1)}%</td>`;
              tbody.appendChild(tr);
            }
            const rangeEl = document.getElementById('recent-window-range');
            if (rangeEl){
              if (flattened.length>0){
                const lastDate = new Date(flattened[flattened.length-1].dateISO||Date.now());
                const firstDate = new Date(flattened[0].dateISO||Date.now());
                rangeEl.textContent = `${flattened.length}문항 · ${lastDate.toLocaleDateString()} – ${firstDate.toLocaleDateString()}`;
              } else {
                rangeEl.textContent = '';
              }
            }
          }
        } catch(e){ console.warn('recent wrong by book error', e); }

        // 5) 랭킹 불러오기
        try {
          const lb = await apiGet('/leaderboard');
          const tbody = document.getElementById('table-leaderboard');
          if (tbody){
            tbody.innerHTML = '';
            (lb?.leaders||[]).forEach((row, idx)=>{
              const tr = document.createElement('tr');
              tr.innerHTML = `<td class="text-center">${idx+1}</td><td>${row.username}</td><td class="text-end">${row.avgPercent.toFixed(1)}%</td><td class="text-end">${row.sampleCount}</td>`;
              tbody.appendChild(tr);
            });
          }
        } catch(e){ console.warn('leaderboard error', e); }

      } catch(e){ console.warn('summary/stats build error', e); }
    }

    // 초기 로드
    document.addEventListener('DOMContentLoaded', async ()=>{
      bindAuthUI();
      await refreshWhoAmI();
      // 서버 기본 verses.csv 자동 로드
      await loadDefaultVersesFromServer(false);
      showView('home');
      buildDashboard();
      // 첫 로드 시 점수 표시 기본 숨김(시험 디폴트 가정)
      setScoreVisibility(false);
      refreshMeta();
    });

    // 인라인 onclick에서 쓰는 함수들을 전역(window)에 노출
    window.showView = showView;
    window.saveSettings = saveSettings;
    window.loadSettings = loadSettings;
    window.clearAll = clearAll;
    window.prevQuestion = prevQuestion;
    window.nextQuestion = nextQuestion;
    window.submitAnswer = submitAnswer;
    window.skipQuestion = skipQuestion;
    window.retakeSession = retakeSession;
    window.retakeWrongOnly = retakeWrongOnly;
    window.startTop20Quiz = startTop20Quiz;
  </script>
  <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""

# ------------------------------
# Flask 라우트 (API)
# ------------------------------
@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html; charset=utf-8")

@app.route("/whoami")
def whoami():
    uname = current_username()
    return jsonify({"logged_in": bool(uname), "username": uname or None})

@app.route("/signup", methods=["POST"])
def signup():
    payload = request.get_json(force=True)
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    if not username or not password:
        return jsonify({"ok": False, "error": "missing username/password"}), 400
    db = load_db()
    if username in db["users"] and db["users"][username].get("pw_hash"):
        return jsonify({"ok": False, "error": "이미 존재하는 아이디입니다."}), 400
    ensure_user(db, username)
    db["users"][username]["pw_hash"] = generate_password_hash(password)
    save_db(db)
    session["username"] = username
    return jsonify({"ok": True, "username": username})

@app.route("/login", methods=["POST"])
def login():
    payload = request.get_json(force=True)
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    if not username or not password:
        return jsonify({"ok": False, "error": "missing username/password"}), 400
    db = load_db()
    user = db["users"].get(username)
    if not user or not user.get("pw_hash") or not check_password_hash(user["pw_hash"], password):
        return jsonify({"ok": False, "error": "아이디 또는 비밀번호가 올바르지 않습니다."}), 400
    session["username"] = username
    return jsonify({"ok": True, "username": username})

@app.route("/logout", methods=["POST"])
def logout():
    session.pop("username", None)
    return jsonify({"ok": True})

@app.route("/data")
def data():
    db = load_db()
    uname = current_username()
    if not uname:
        # 비로그인: 빈 사용자 데이터 형태 반환
        return jsonify({"sessions": [], "settings": DEFAULT_SETTINGS.copy(), "verseScores": {}})
    u = ensure_user(db, uname)
    return jsonify({
        "sessions": u.get("sessions", []),
        "settings": u.get("settings", DEFAULT_SETTINGS.copy()),
        "verseScores": u.get("verseScores", {})
    })

@app.route("/save", methods=["POST"])
def save():
    if not require_login():
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    payload = request.get_json(force=True)
    if not payload or "session" not in payload:
        return jsonify({"ok": False, "error": "missing session"}), 400

    uname = current_username()
    db = load_db()
    u = ensure_user(db, uname)

    session_obj = payload["session"]
    replace_id = payload.get("replaceId")  # ← 덮어쓰기 대상 id (선택)

    u.setdefault("sessions", [])
    u.setdefault("verseScores", {})

    # verseScores 업데이트: 정답 -1, 오답/스킵 +1 (최소 0 보장)
    for d in session_obj.get("details", []):
        key = f"{d.get('book')}|{d.get('chapter')}|{d.get('verse')}"
        cur = int(u["verseScores"].get(key, 0))
        if d.get("skipped") or not d.get("correct"):
            cur += 1
        else:
            cur = max(0, cur - 1)
        u["verseScores"][key] = cur

    # 세션 저장(덮어쓰기 or append)
    if replace_id:
        idx = next((i for i, s in enumerate(u["sessions"]) if s.get("id")==replace_id), -1)
        if idx >= 0:
            session_obj["id"] = replace_id
            u["sessions"][idx] = session_obj
        else:
            u["sessions"].append(session_obj)
    else:
        u["sessions"].append(session_obj)

    save_db(db)
    return jsonify({"ok": True})

@app.route("/settings", methods=["POST"])
def set_settings():
    if not require_login():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    st = request.get_json(force=True)
    db = load_db()
    u = ensure_user(db, current_username())
    # ★ 사용자가 보낸 enabledQTypes를 그대로 저장(강제 추가 금지)
    u["settings"] = {
        "numQuestions": int(st.get("numQuestions", 30)),
        "enabledQTypes": list(st.get("enabledQTypes", []))
    }
    save_db(db)
    return jsonify({"ok": True})

@app.route("/reset", methods=["POST"])
def reset():
    if not require_login():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    db = load_db()
    u = ensure_user(db, current_username())
    u["sessions"] = []
    u["settings"] = DEFAULT_SETTINGS.copy()
    u["verseScores"] = {}
    save_db(db)
    return jsonify({"ok": True})

# ------------------------------
# 삭제/정리 API
# ------------------------------
@app.route("/delete_session", methods=["POST"])
def delete_session():
    if not require_login():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    payload = request.get_json(force=True)
    sid = payload.get("id")
    if not sid:
        return jsonify({"ok": False, "error":"missing id"}), 400
    db = load_db()
    u = ensure_user(db, current_username())
    before = len(u["sessions"])
    u["sessions"] = [s for s in u["sessions"] if s.get("id") != sid]
    save_db(db)
    return jsonify({"ok": True, "deleted": before - len(u["sessions"])})

@app.route("/delete_verse_score", methods=["POST"])
def delete_verse_score():
    if not require_login():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    payload = request.get_json(force=True)
    key = payload.get("key")
    if not key:
        return jsonify({"ok": False, "error":"missing key"}), 400
    db = load_db()
    u = ensure_user(db, current_username())
    if key in u["verseScores"]:
        # 0으로 리셋하거나 완전 삭제
        u["verseScores"].pop(key, None)
    save_db(db)
    return jsonify({"ok": True})

@app.route("/clear_top20", methods=["POST"])
def clear_top20():
    if not require_login():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    db = load_db()
    u = ensure_user(db, current_username())
    u["verseScores"] = {}  # 전체 초기화
    save_db(db)
    return jsonify({"ok": True})

# ------------------------------
# 리더보드 API
# ------------------------------
@app.route("/leaderboard")
def leaderboard():
    db = load_db()
    leaders = []
    for uname, u in db.get("users", {}).items():
      # u가 dict인지, sessions가 list인지 확인
      if not isinstance(u, dict):
          continue
      sessions = u.get("sessions", [])
      # 시험 세션만, 그리고 10문항 이상
      exams = [se for se in sessions if (se.get("type")=='exam' or (se.get("total",0)>1)) and (se.get("total",0) >= 10)]
      if not exams:
          continue
      # 날짜 최신순
      exams.sort(key=lambda x: x.get("dateISO",""), reverse=True)
      recent5 = exams[:5]
      if not recent5:
          continue
      # 평균 퍼센트
      total_pct = 0.0
      cnt = 0
      for se in recent5:
          t = max(1, se.get("total",1))
          c = se.get("correct",0)
          total_pct += (c/t)*100.0
          cnt += 1
      if cnt == 0:
          continue
      avg_pct = total_pct / cnt
      leaders.append({"username": uname, "avgPercent": avg_pct, "sampleCount": cnt})
    # 정렬: 평균 내림차순, 그 다음 샘플수, 그 다음 이름
    leaders.sort(key=lambda r: (-r["avgPercent"], -r["sampleCount"], r["username"]))
    # 상위 10만
    leaders = leaders[:10]
    return jsonify({"ok": True, "leaders": leaders})

# ------------------------------
# 서버 verses.csv API
# ------------------------------
@app.route("/verses")
def get_verses():
    reload_flag = request.args.get("reload")
    if reload_flag:
        load_server_verses_file()
    return jsonify({
        "ok": True,
        "verses": SERVER_VERSES,
        "meta": {
            "count": len(SERVER_VERSES),
            "source": SERVER_VERSES_SOURCE
        }
    })


if __name__ == "__main__":
    # 개발 편의: 시작 시 한번 더 로드(환경변수 경로 변경 반영)
    load_server_verses_file()
    app.run(host="127.0.0.1", port=5000, debug=True)
