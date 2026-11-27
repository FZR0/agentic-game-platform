"use strict";

// 所有遊戲共用的對局頁：對局流程、AI 教練面板、復盤、提問、歷史對局與設定。
// 遊戲本身只透過 window.GameBoard（各遊戲的 board.js）：draw、pointAt、canPlay、emptyFrame、extraActions，
// 以及可省略的 actionText（行動的顯示文字）、turnHint（輪到玩家時的提醒）、
// renderControls（依局面產生動作按鈕，例如撲克的跟注、加注）。
// 局面一律用伺服器送來的 frame（第 i 步之後的局面），網頁不重算規則。

const CONFIG = JSON.parse(document.getElementById("game-config").textContent);
const MODEL_KEY = "arena.model";
const COACH_KEY = "arena.coach";
const STEPS_KEY = `arena.steps.${CONFIG.key}`;
const COORD_RE = new RegExp(CONFIG.coordPattern, "g");

const canvas = document.getElementById("board");
const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
const $ = (id) => document.getElementById(id);
// 撲克開放牌桌（2026-11-16）的真人「座位」是成員代號（1000＋帳號編號，見 holdem.rules.MEMBER），畫面上寫他現在坐的座位
const seatName = (seat) => {
  if (seat >= 1000) {
    const s = state.frames.at(-1)?.seats?.find((x) => x.token === seat);
    return s ? `${s.seat} 號位` : "觀戰席";
  }
  return CONFIG.seatNames[seat] ?? `座位 ${seat}`;
};
const actionText = (action) => GameBoard.actionText?.(action) ?? action;
const STEP = CONFIG.stepWord; // 「第幾手」的單位：棋類是「手」，撲克是「步」（「第 3 手」在撲克是第 3 手牌）
// 頂端列的下拉面板，同時只開一個；參考表（撲克的勝率表）只有部分遊戲有
const POPOVERS = ["rules-panel", "reference-panel", "settings-panel"].filter((id) => $(id));
const REVIEW_HINT = "對局結束後按上方的「復盤」，會檢查你的每一步，列出關鍵時刻和小失誤。";

const state = {
  round: 0, // 每次開新局或打開舊局加一，用來略過舊的說明
  gameId: null, // 資料庫中的對局編號
  settings: {},
  actions: [],
  frames: [], // frames[i]＝第 i 步之後的局面；frames[0] 是開局
  human: 1,
  over: false,
  busy: false,
  reviewing: false,
  viewAt: 0, // 復盤時顯示第幾步之後的局面
  marks: null, // 復盤時標示 {played, best, seat}
  showNumbers: false,
  hover: null, // 滑鼠所在的可下點，顯示預覽
  pending: null, // 玩家剛下、伺服器還沒回應的一手，先畫出來
  highlight: null, // 點選說明中的座標後固定標示的點
  hoverHighlight: null, // 滑過說明中的座標時暫時標示的點
  unread: 0, // AI 教練收起時累積的新思路數
  hiddenNotes: false, // 有被遮住的 AI 思路（撲克的底牌），復盤時要重新載入
  pvp: false, // 真人對戰（2026-11-05）：對手也是真人，對局中不能用 AI 教練
  players: {}, // 真人對戰的 {座位: 使用者名稱}；null 是還在等人加入
  spectators: [], // 觀戰的人的使用者名稱（玩家列表用，2026-11-13）
  hostSeat: null, // 房主坐的座位（玩家列表標「房主」；房主觀戰中是 null）
  ownerName: null, // 房主的使用者名稱（2026-11-18 房主可以觀戰，認房主用名稱）
  closed: false, // 房主在開始前關閉了這個房間（2026-11-13）
  watching: false, // 觀戰中（2026-11-08）：沒有座位，不能下、不能用 AI 教練
  owner: false, // 自己是房主（伺服器的 is_owner，可以轉讓）：開始前改設定會套用到同一個房間
  startsAt: null, // 真人對戰的開始時間（這台電腦的時鐘，2026-11-13）；null＝房主還沒按「開始」
  starting: false, // 房主按了「開始」、請求還沒回來
  chat: [], // 房間聊天室看得到的訊息
  chatSeen: 0, // 聊天室收起時已經看過的最後一則訊息編號，用來算未讀
  chatSending: false,
};
// 說明一次只送一個請求給 Ollama，依序產生
let narrationQueue = Promise.resolve();
// 真人對戰打完、幾秒後回到房間（下一盤）的計時器（2026-11-15）；null＝沒有要回去
let returnTimer = null;
const RETURN_DELAY_MS = 4000;

function storageGet(key) {
  try {
    return localStorage.getItem(key);
  } catch (err) {
    return null;
  }
}

function storageSet(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch (err) {
    // 無法記住偏好時，本次仍可使用
  }
}

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken },
    body: JSON.stringify(body),
  };
  const res = await fetch(`/api/${CONFIG.key}/${path}`, options);
  // 登入過期時伺服器把請求轉到登入頁（2026-11-05 起要登入），直接帶玩家去登入
  if (res.redirected && new URL(res.url).pathname.startsWith("/accounts/login/")) {
    window.location.href = `/accounts/login/?next=${encodeURIComponent(window.location.pathname)}`;
    throw new Error("請先登入");
  }
  let data;
  try {
    data = await res.json();
  } catch (err) {
    // 看不到的對局（不在房間裡、被請離開）是 404 網頁，不是伺服器出錯（2026-11-20）
    if (res.status === 404) throw new Error("找不到這盤對局，或你已經不在這個房間裡");
    // 伺服器程式出錯時回傳的是錯誤網頁、不是 JSON；給玩家看得懂的訊息，細節在伺服器視窗
    throw new Error(`伺服器發生錯誤（${res.status}），這一步沒有存檔`);
  }
  if (!res.ok) throw new Error(data.error || res.statusText);
  showProblem(data.ai_problem);
  // 查得出 AI 伺服器的問題時，說明已經講了，不再重複一般的提示
  if (!data.ai_problem) showNotice(data.handled?.notice);
  return data;
}

// AI 伺服器出問題時的說明（2026-10-30）：查得出原因寫排除步驟，查不出寫可能原因並附故障報告。
// 結構和 templates/arena/_ai_problem.html 相同；問題解決時伺服器回傳 null，就收起來
function showProblem(p) {
  const el = $("ai-problem");
  el.hidden = !p;
  if (!p || el.dataset.report === p.report) return;
  el.dataset.report = p.report;
  const para = (text, className = "") => Object.assign(document.createElement("p"), { textContent: text, className });
  const list = (tag, items) => {
    const l = document.createElement(tag);
    l.append(...items.map((text) => Object.assign(document.createElement("li"), { textContent: text })));
    return l;
  };
  const summary = Object.assign(document.createElement("summary"), { textContent: `AI 暫時無法使用：${p.title}` });
  const body = Object.assign(document.createElement("div"), { className: "ai-problem-body" });
  if (p.detail) body.append(para(p.detail));
  if (p.known) {
    body.append(para("排除步驟："), list("ol", p.steps));
  } else {
    const where = p.report_file ? `（也存在 ${p.report_file}）` : "";
    body.append(para("可能原因："), list("ul", p.causes), para(`故障報告${where}：`),
      Object.assign(document.createElement("pre"), { className: "ai-report", textContent: p.report }));
  }
  body.append(para(`這段期間電腦照常下棋，說明改顯示分析原文。修好後不用重新啟動網站，約 ${p.retry_minutes} 分鐘內會自動恢復。`, "muted"));
  el.replaceChildren(summary, body);
}

// AI 助理沒完成、改用一般方式處理時，伺服器回傳給玩家看的提示（2026-10-28）
let noticeTimer = null;

function showNotice(text) {
  if (!text) return;
  const el = $("assistant-notice");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { el.hidden = true; }, 10000);
}

// ---- 棋盤 ----

const currentIndex = () => (state.reviewing ? state.viewAt : state.frames.length - 1);
const currentFrame = () => state.frames[currentIndex()];

function draw() {
  if (!state.frames.length) return;
  const n = currentIndex();
  GameBoard.draw(canvas, {
    frame: state.frames[n],
    actions: state.actions.slice(0, n),
    showNumbers: state.showNumbers,
    hover: state.hover,
    hoverSeat: state.human,
    pending: state.pending,
    marks: state.reviewing ? state.marks : null,
    spot: state.hoverHighlight || state.highlight,
  });
}

// ---- AI 教練面板 ----

function setCoachOpen(open) {
  $("coach").hidden = !open;
  $("app").classList.toggle("coach-open", open);
  $("coach-toggle").setAttribute("aria-expanded", String(open));
  if (open) setUnread(0);
  storageSet(COACH_KEY, open ? "1" : "0");
}

function setUnread(n) {
  state.unread = n;
  $("coach-badge").hidden = n === 0;
  $("coach-badge").textContent = String(n);
}

function selectTab(name) {
  for (const button of document.querySelectorAll(".tabs button")) {
    const selected = button.dataset.tab === name;
    button.setAttribute("aria-selected", String(selected));
    $(`tab-${button.dataset.tab}`).hidden = !selected;
  }
}

function updateAskAvailability() {
  const enabled = state.reviewing && state.gameId !== null && !asking && !state.watching;
  document.querySelectorAll("#ask-form button, #ask-form input, .quick-questions button")
    .forEach((el) => { el.disabled = !enabled; });
  if (!state.reviewing) $("ask-context").textContent = "復盤時可以針對棋盤上的任何局面提問。";
}

// 頂端列的「歷史對局」「設定」下拉面板：同時只開一個，點外面或按 Esc 關閉
function setPopover(id, open) {
  $(id).hidden = !open;
  document.querySelector(`[aria-controls="${id}"]`).setAttribute("aria-expanded", String(open));
}

function closePopovers(except) {
  for (const id of POPOVERS) {
    if (id !== except) setPopover(id, false);
  }
}

// ---- 對局 ----

let statusTimer = null;

function setStatus(text) {
  clearInterval(statusTimer);
  $("game-status").textContent = state.watching ? `觀戰中：${text}` : text;
}

// Agent 一手要十幾秒，顯示經過的秒數，讓人知道它還在查證
function setThinkingStatus() {
  const started = Date.now();
  setStatus("AI 思考中…");
  statusTimer = setInterval(() => {
    $("game-status").textContent = `AI 思考中… ${Math.round((Date.now() - started) / 1000)} 秒`;
  }, 1000);
}

// 牌局在進行，不管輪到誰（撲克真人對戰的離場不必輪到自己，2026-11-16）
const inPlay = () => !state.over && !state.reviewing && !waitingForPlayers() && roomStarted();
const humanTurn = () => inPlay() && currentFrame()?.to_move === state.human;
// 房主請 AI 坐的座位（2026-11-18）不算空位
const waitingForPlayers = () => state.pvp
  && Object.entries(state.players).some(([seat, name]) => name === null && !state.aiSeats.includes(Number(seat)));
// 真人對戰要房主按「開始」、倒數結束才能下（伺服器也會拒絕，2026-11-13）
const roomStarted = () => !state.pvp || (state.startsAt !== null && Date.now() >= state.startsAt);
// 真人對戰還沒結束時不能用 AI 教練（伺服器也會拒絕），否則就是幫其中一方作弊（使用者決定，2026-11-05）
const coachLocked = () => state.pvp && !state.over;

// 坐在真人對戰房間裡（對局中、大廳、打完等著回到房間）：頂端的「復盤」換成「對戰紀錄」（2026-11-16 使用者要求），
// 打開這個房間打完的每一盤，雙方各自從那裡復盤；從對局紀錄打開某一盤復盤時仍是「復盤」
// 撲克開放牌桌（使用者要求「補入式」，2026-11-16）：上桌／排隊、觀眾、補 AI，一手結束後自動補人發牌
const isTable = () => Boolean(state.pvp && state.settings?.table);
const TABLE_DELAY_MS = 5000; // 一手結束後等幾秒補人、發下一手（operations.TABLE_DELAY）
let dealTimer = null;
let dealFor = null; // 已經排好（或送出）發牌的那一手，同一手只送一次

// 一手牌結束（桌上還有兩個人以上有籌碼）時，等幾秒請伺服器補人、發下一手。房間裡每個人的網頁都會送，
// 伺服器只發一次（看 hand）；所有人都關掉網頁時牌桌停在這裡，有人回來就接著發（使用者同意，2026-11-16）
function scheduleDeal() {
  const frame = state.frames.at(-1);
  if (!isTable() || state.over || state.reviewing || state.closed || !roomStarted() || !frame?.hand_over
      || !frame.hand || frame.live < 2 || dealFor === frame.hand) return;
  dealFor = frame.hand;
  const round = state.round;
  clearTimeout(dealTimer);
  dealTimer = setTimeout(async () => {
    if (round !== state.round || state.frames.at(-1)?.hand !== dealFor) return;
    try {
      await api(`games/${state.gameId}/deal/`, { hand: dealFor, ...agentOptions() });
    } catch (err) {
      setStatus(`沒有發下一手：${err.message}`);
    }
  }, TABLE_DELAY_MS);
}

const inRoom = () => state.pvp && !state.watching && !state.closed && !state.reviewing && state.gameId !== null
  && (!state.over || returnTimer !== null);

function updateButtons() {
  $("new-game").disabled = state.busy;
  const review = $("start-review");
  review.textContent = inRoom() ? "對戰紀錄" : "復盤";
  // 觀戰的人沒有座位，復盤是從某一方的角度分析的，所以不能用（結束後可以用 ◀ ▶ 回看）
  // 房主在開始前關掉的房間一步都沒下，沒有東西可以復盤
  review.disabled = !inRoom() && (state.busy || state.reviewing || state.gameId === null || coachLocked()
    || state.watching || state.closed || returnTimer !== null);
  // 房間裡（真人對戰的頁面，從「對戰紀錄」打開的復盤除外）完全不出現 AI 教練（2026-11-16 使用者要求「隱藏＋停用，到非房間才能打開」）
  const roomPage = state.pvp && !state.reviewing;
  $("coach-toggle").hidden = roomPage;
  $("coach-toggle").disabled = roomPage || coachLocked();
  $("coach-toggle").title = coachLocked() ? "真人對戰中不能使用 AI 教練，對局結束後就可以復盤" : "";
  if (roomPage && !$("coach").hidden) setCoachOpen(false);
  document.querySelectorAll("#game-actions button").forEach((b) => { b.disabled = state.busy || !humanTurn(); });
  // 「結束（不計勝負）」只在有存檔、還沒結束的對局顯示；玩家先下而還沒下第一步時沒有存檔，不必結束。
  // 真人對戰不能不計勝負（快輸的一方按了就能讓輸棋不算），要結束請認輸
  $("end-game").hidden = state.gameId === null || state.over || state.reviewing || state.pvp;
  // 撲克真人房間的房主禁用了勝率表（2026-11-18）：按鈕收起來，打開著的也關掉
  if ($("reference-toggle")) {
    const banned = state.pvp && state.settings?.equity_table === "hide";
    $("reference-toggle").closest(".menu").hidden = banned;
    if (banned && !$("reference-panel").hidden) setPopover("reference-panel", false);
  }
  $("end-game").disabled = state.busy;
  $("room-start").hidden = !(state.pvp && state.owner && state.gameId !== null
    && !state.over && !waitingForPlayers() && state.startsAt === null);
  const seated = tableSeated().length;
  $("start-room").disabled = state.busy || state.starting || (isTable() && seated < 2);
  $("room-start").querySelector("p").textContent = isTable()
    ? (seated < 2 ? "牌桌上要有兩個人以上（可以補 AI）才能開始。" : `牌桌上有 ${seated} 人。準備好就按「開始」，倒數 3 秒後開始。`)
    : "大家都到齊了。準備好就按「開始」，倒數 3 秒後開始。";
  // 房主在房間大廳換遊戲（2026-11-15）
  $("room-game-field").hidden = !(canChangeRoom() || canSwitchTable());
  $("room-game").disabled = state.busy;
  const locked = roomSettingsLocked();
  document.querySelectorAll("#opponent, #human-seat, .game-setting").forEach((el) => { el.disabled = locked; });
  $("room-settings-note").hidden = !locked;
  $("room-settings-note").textContent = canSwitchTable()
    ? "牌局進行中：換遊戲會先結束這場牌局（不計勝負）；其他設定要按「結束牌局」回到房間大廳才能改。"
    : state.owner ? "這一盤已經開始了，設定要等打完回到房間才能改。" : "房間的設定由房主決定。";
  // 離開房間：觀戰的人隨時可以走；坐在座位上的人只在開始前（對局中要認輸）
  // 撲克開放牌桌：沒坐在桌上就能走（坐著的人先按「觀眾」或「離場」）
  $("leave-room").hidden = !(state.pvp && state.gameId !== null && !state.closed
    && (state.watching || inLobby() || (isTable() && !state.over && myTableSeat() === null)));
  $("leave-room").disabled = state.busy || state.starting;
  updateChatAvailability();
  renderRoomPlayers();
  GameBoard.renderControls?.(currentFrame(), $("game-actions"), !state.busy && humanTurn(), playAction,
    !state.busy && !state.watching && inPlay());
  updateAskAvailability();
  scheduleDeal();
}

function agentOptions() {
  return { model: $("model").value, steps: Number($("steps").value) || CONFIG.defaultSteps };
}

function readSettings() {
  const settings = {};
  document.querySelectorAll(".game-setting").forEach((el) => {
    settings[el.dataset.name] = Number.isNaN(Number(el.value)) ? el.value : Number(el.value);
  });
  if (pvpChosen() && CONFIG.pickHumanSeats) {
    settings.humans = [1, ...[...document.querySelectorAll("#human-seat-boxes input:checked")].map((b) => Number(b.value))];
  }
  return settings;
}

const pvpChosen = () => $("opponent").value === "pvp";

// 撲克的真人對戰：依人數列出 2 號位以後的座位，勾選的給朋友坐（預設勾 2 號位）
function renderHumanSeatBoxes() {
  const box = $("human-seat-boxes");
  const players = Number(document.querySelector('.game-setting[data-name="players"]')?.value) || 2;
  const checked = new Set([...box.querySelectorAll("input:checked")].map((b) => Number(b.value)));
  if (!checked.size) checked.add(2);
  box.replaceChildren(...Array.from({ length: players - 1 }, (_, i) => {
    const seat = i + 2;
    const label = document.createElement("label");
    const input = Object.assign(document.createElement("input"), { type: "checkbox", value: String(seat), checked: checked.has(seat) });
    label.append(input, ` ${seat} 號位`);
    return label;
  }));
}

function updateOpponentFields() {
  $("human-seats").hidden = !(pvpChosen() && CONFIG.pickHumanSeats);
  // 只有真人房間的設定（撲克的勝率表，2026-11-18）
  document.querySelectorAll("[data-pvp-only]").forEach((el) => { el.hidden = !pvpChosen(); });
  if (!$("human-seats").hidden) renderHumanSeatBoxes();
}

function resetView(human, settings, actions, frames, pvp = false, players = {}, watching = false) {
  state.round += 1;
  closeSocket();
  clearTimeout(returnTimer);
  returnTimer = null;
  Object.assign(state, {
    human, settings, actions, frames, over: false, reviewing: false, viewAt: 0, marks: null,
    hover: null, highlight: null, hoverHighlight: null, hiddenNotes: false, pvp, players, watching, owner: false,
    startsAt: null, starting: false, spectators: [], aiSeats: [], hostSeat: null, ownerName: null, closed: false, locked: false, roomPassword: "", table: null, myToken: null,
  });
  stopCountdown(); // 聊天不清掉：房主改房間設定時仍是同一個房間；開新局時由 newGame 清掉
  $("invite").hidden = true;
  if (pvp) setCoachOpen(false);
  setUnread(0);
  $("ai-log").replaceChildren();
  $("ai-log").classList.remove("clickable");
  $("thoughts-empty").hidden = false;
  $("thoughts-empty").textContent = "AI 每下一手，思路會出現在這裡。復盤時點一則思路，棋盤會跳到那一手。";
  $("moments").replaceChildren();
  $("review-summary").textContent = REVIEW_HINT;
  $("answers").replaceChildren();
  $("ask-welcome").hidden = false;
  $("review-nav").hidden = true;
  $("mark-legend").hidden = true;
  selectTab("thoughts");
  draw();
}

const AI_STEP_MS = 600; // 多位 AI 連續行動（撲克）時，每一步停一下，玩家才看得出誰做了什麼

async function applyAiMoves(moves) {
  if (moves.length > 1) setStatus("其他玩家行動中…");
  // 遊戲可以告訴我們一步的動畫要多久（GameBoard.stepDelay，撲克有），播完才顯示下一步；先等玩家自己那一步播完
  const delay = (prev, next) => GameBoard.stepDelay?.(prev, next) ?? 0;
  let wait = delay(state.frames.at(-2), state.frames.at(-1));
  for (const m of moves) {
    if (wait > 0) await new Promise((resolve) => setTimeout(resolve, wait));
    const prev = state.frames.at(-1);
    state.actions.push(m.action);
    state.frames.push(m.frame);
    addNote(m, true);
    draw();
    wait = Math.max(AI_STEP_MS, delay(prev, m.frame));
  }
}

// nextRound：真人對戰打完時伺服器開好的下一盤（同一個房間），幾秒後大家一起回到房間（2026-11-15）
function finishGame(winner, draw, nextRound = null) {
  state.over = true;
  const who = state.pvp && state.players[winner] ? state.players[winner] : "AI";
  // 撲克開放牌桌不計勝負，由房主結束（2026-11-16）
  const result = draw ? "和局。" : winner === null || winner === undefined ? "牌局結束（不計勝負）。"
    : winner === state.human && !state.watching ? "你贏了！" : `${seatName(winner)}（${who}）獲勝。`;
  const detail = currentFrame()?.result ? `（${currentFrame().result}）` : "";
  const tail = nextRound ? `${RETURN_DELAY_MS / 1000} 秒後回到房間準備下一盤；這一盤可以到「對局紀錄」復盤。`
    : state.watching ? "可以用 ◀ ▶ 一步一步回看。" : "可以按上方的「復盤」檢討。";
  setStatus(`${result}${detail}${tail}`);
  if (nextRound) returnToRoom(nextRound, `上一盤：${result}${detail}`);
}

function returnToRoom(nextId, lastResult) {
  if (returnTimer !== null) return;
  const mode = state.watching ? "watch" : "resume";
  returnTimer = setTimeout(async () => {
    returnTimer = null;
    await openGame(nextId, mode);
    // 打不開（2026-11-22 例行檢查）：房主在撲克牌局中換遊戲時先結束牌局、馬上把下一盤換成別的遊戲，
    // 這裡用撲克的網址讀不到那一盤（404），非房主停在「無法打開對局」。改用網址打開，伺服器（views._opening）轉到對的遊戲頁
    if (state.gameId !== nextId) {
      window.location.replace(`/${CONFIG.key}/?${mode}=${nextId}`);
      return;
    }
    showNotice(`${lastResult}這一盤可以到「對局紀錄」復盤。`);
  }, RETURN_DELAY_MS);
  updateButtons();
}

function yourTurnStatus() {
  const hint = GameBoard.turnHint?.(currentFrame()) || "";
  setStatus(`輪到你下（${seatName(state.human)}）${hint ? `。${hint}` : ""}`);
}

function statusAfterMove(data) {
  if (data.closed) {
    roomClosed();
    return;
  }
  if (data.winner || data.draw || data.ended) finishGame(data.winner, data.draw, state.pvp ? data.next_round : null);
  else if (waitingForPlayers()) {
    setStatus(state.owner
      ? "等朋友加入中…把房間編號傳給朋友（上鎖的話連房間密碼一起）。按「開始」之前都可以在「設定」改設定或換遊戲，改了馬上套用到這個房間。"
      : "等朋友加入中…大家到齊後，由房主按「開始」。");
  }
  else if (isTable() && state.startsAt === null) {
    const seat = myTableSeat() === null ? "按「上桌」坐下（座位隨機）。" : "你已經上桌了。";
    setStatus(state.owner ? `${seat}房主可以補 AI；牌桌上有兩個人以上就能按「開始」。` : `${seat}等房主按「開始」…`);
  }
  else if (state.pvp && state.startsAt === null) {
    setStatus(state.owner ? "大家都到齊了，按「開始」開始對局。" : "大家都到齊了，等房主按「開始」…");
  }
  else if (!roomStarted()) setStatus("倒數中，馬上開始…");
  else if (isTable() && currentFrame()?.hand_over) {
    const f = currentFrame();
    setStatus(f.live < 2
      ? `${f.hand_result}等待中：牌桌上不到兩個人，按「＋ 排隊上桌」，或由房主補 AI。`
      : `${f.hand_result}幾秒後補上排隊的人、發下一手。`);
  }
  else if (currentFrame()?.to_move !== state.human) {
    const name = state.aiSeats.includes(currentFrame()?.to_move) ? "AI" : state.players[currentFrame()?.to_move];
    setStatus(name ? `等 ${name}（${seatName(currentFrame().to_move)}）下…` : "其他玩家行動中…");
  } else yourTurnStatus();
}

// ---- 真人對戰（2026-11-05）：邀請朋友、WebSocket 即時同步 ----

function showInvite(code) {
  if (state.watching && !isTable() && !state.owner) code = ""; // 房主觀戰時也要能邀人（撲克開放牌桌的房間裡的人都能看密碼）
  $("invite").hidden = !code;
  if (!code) return;
  $("room-code").textContent = code;
  $("invite-link").value = `${window.location.origin}/join/${code}/`;
}

let socket = null;
let socketRetry = null;
let pendingUpdate = null; // 自己的請求還沒回來時收到的推送，等請求回來再套用（否則同一步會加兩次）

function closeSocket() {
  clearTimeout(socketRetry);
  if (socket) {
    socket.onclose = null;
    socket.close();
  }
  socket = null;
}

// 連到 ws/games/<編號>/：有人下了一步或加入時，伺服器送來這個座位看到的整盤棋；斷線時每 3 秒重連一次
function openSocket(gameId) {
  closeSocket();
  const round = state.round;
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${scheme}://${window.location.host}/ws/games/${gameId}/`);
  socket.onmessage = (event) => {
    if (round !== state.round) return;
    const data = JSON.parse(event.data);
    if (data.type === "chat") {
      renderChat(data.chat); // 聊天不會加步數，不必等自己的請求回來
      return;
    }
    if (data.type === "left") { // 房主請你離開了房間（2026-11-15）
      leftRoom();
      return;
    }
    if (data.type !== "update") return;
    if (state.busy) pendingUpdate = data;
    else applyUpdate(data);
  };
  socket.onclose = () => {
    if (round === state.round && !state.over) socketRetry = setTimeout(() => openSocket(gameId), 3000);
  };
}

// 伺服器推送的整盤棋（operations.game_detail）：只加上這裡還沒有的步數，所以重複收到也沒關係
async function applyUpdate(data) {
  const seat = data.my_seat ?? null; // null＝觀戰
  // 房主換了別的遊戲（2026-11-15）：到那個遊戲的頁面打開同一個房間
  if (data.game.game_type !== CONFIG.key) {
    window.location.href = `/${data.game.game_type}/?${seat === null ? "watch" : "resume"}=${data.game.id}`;
    return;
  }
  // 房主改了設定（2026-11-08），或在房間裡換了座位、坐下、起身觀戰（2026-11-15）：
  // 棋盤、座位都可能不同，整盤換成伺服器的版本
  const rebuild = JSON.stringify(data.game.settings) !== JSON.stringify(state.settings)
    || data.actions.length < state.actions.length || seat !== (state.watching ? null : state.human);
  if (rebuild) {
    resetView(seat, data.game.settings, data.actions, data.frames, true, data.game.players || {}, seat === null);
    state.gameId = data.game.id;
    rememberRoom();
    openSocket(data.game.id);
  }
  state.owner = Boolean(data.is_owner); // 房主可以轉讓，所以每次都照伺服器說的
  state.roomPassword = data.room_password ?? ""; // 房間鎖的密碼，只有房主拿得到（2026-11-18）
  state.myToken = data.my_token ?? null;
  if (rebuild) syncSettingsForm(seat, data.game.settings); // 不能改的時候也顯示房間的設定（roomSettingsLocked 鎖住）
  const wasWaiting = waitingForPlayers();
  setRoomPeople(data.game);
  // 大家到齊後就不能改設定了；開房時自動打開的設定面板會蓋住「開始」按鈕，所以收起來
  if (wasWaiting && !waitingForPlayers()) setPopover("settings-panel", false);
  showInvite(data.game.room_code);
  setStartsIn(data.game.starts_in);
  // 每一步都重新看目前有幾步：前一次推送還在一步一步播時又收到新的推送，兩邊都不會重複加同一步
  while (state.actions.length < data.actions.length) {
    const i = state.actions.length;
    state.actions.push(data.actions[i]);
    state.frames.push(data.frames[i + 1]);
    const note = data.notes.find((n) => n.index === i);
    if (note) addNote(note, true);
    draw();
    if (i + 1 < data.actions.length) await new Promise((resolve) => setTimeout(resolve, AI_STEP_MS));
  }
  statusAfterMove(data.game);
  updateButtons();
  // 觀戰中對局結束了；有下一盤時是回到房間（finishGame），不用回看
  if (state.watching && state.over && !state.reviewing && !state.closed && !data.game.next_round) watchReplay();
}

// 觀戰的人不能用 AI 復盤（沒有座位，分析是從某一方的角度寫的），對局結束後只能一步一步回看
function watchReplay() {
  Object.assign(state, { over: true, reviewing: true, viewAt: state.actions.length });
  closeSocket();
  $("review-nav").hidden = false;
  $("ai-log").classList.add("clickable");
  updateButtons();
  updateNav();
  draw();
}

async function flushPendingUpdate() {
  const data = pendingUpdate;
  pendingUpdate = null;
  if (data) await applyUpdate(data);
}

// ---- 房間的玩家列表（2026-11-13 使用者要求）----

function setRoomPeople(game) {
  state.players = game.players || {};
  state.table = game.table || null; // 撲克開放牌桌的隊列、要下桌的座位、房主（2026-11-16）
  state.spectators = game.spectators || [];
  state.aiSeats = game.ai_seats || []; // 房主請 AI 坐的座位（2026-11-18）
  // 房主的使用者名稱與座位：房主可能在觀戰（2026-11-18），所以照伺服器給的名稱找，觀戰中座位是 null
  state.ownerName = game.owner ?? game.table?.owner ?? null;
  const hostSeat = Object.entries(state.players).find(([, name]) => name !== null && name === state.ownerName);
  state.hostSeat = hostSeat ? Number(hostSeat[0]) : null;
  state.closed = Boolean(game.closed);
  state.locked = Boolean(game.locked);
}

function roomButton(label, onClick, className = "") {
  const b = document.createElement("button");
  b.type = "button";
  b.className = `room-action ${className}`.trim();
  b.textContent = label;
  b.disabled = state.busy || state.starting;
  b.addEventListener("click", onClick);
  return b;
}

// 房主把某個人換到別的座位（坐到有人的座位時兩人交換）或改成觀戰；current 是他現在的座位，觀戰中是 null。
// 房主換自己（2026-11-18 使用者要求）：name 傳空字串，current 是房主的座位（觀戰中是 null）。
// 房主也可以改成觀戰（2026-11-18 使用者決定）；只有舊版撲克的房主固定 1 號位，誰都不能換進或換出
function seatPicker(name, current) {
  const select = document.createElement("select");
  select.className = "room-action";
  select.disabled = state.busy || state.starting;
  select.setAttribute("aria-label", name ? `把 ${name} 換到` : "把自己換到");
  const fixed = (s) => CONFIG.pickHumanSeats && s === state.hostSeat && s !== null;
  select.append(new Option("換到…", ""));
  for (const s of Object.keys(state.players).map(Number)) {
    if (s !== current && !fixed(s) && !state.aiSeats.includes(s)) select.append(new Option(seatName(s), String(s)));
  }
  if (current !== null && !fixed(current)) select.append(new Option("觀戰", "0"));
  select.addEventListener("change", () => {
    if (select.value !== "") moveTo(name, Number(select.value));
  });
  return select;
}

// 撲克開放牌桌：最新局面裡坐在桌上的座位（空位不算）、自己坐的座位（沒上桌是 null）
const tableSeated = () => (state.frames.at(-1)?.seats || []).filter((s) => !s.empty);
const myTableSeat = () => (isTable() && !state.watching ? state.frames.at(-1)?.my_table_seat ?? null : null);
const tableAction = (op, extra = {}) => roomAction("table", { op, ...extra, ...agentOptions() }, "牌桌沒有更新");

function aiLabel(entry) {
  return entry.ai === "agent" ? "Agent AI" : "機器人";
}

// ---- 對局玩家與房間成員（2026-11-18 使用者要求「控制面板太亂」，把原本混在一起的玩家列表拆成兩塊）----
// 對局玩家：每個座位一格，只放和座位本身有關的按鈕（坐這裡、改成觀戰、觀眾、請 AI 下桌）。
// 房間成員：房間裡每個人一列，房主的管理（換到…、交給他當房主、請離開）放在那個人的右邊。

// 登入的人的使用者名稱（play.html 的 viewer-name；不是登入用的帳號）：觀戰的人沒有座位，房間成員要靠名字標出「你」
const ME = JSON.parse(document.getElementById("viewer-name")?.textContent || '""');

function badgeList(tags) {
  const box = document.createElement("span");
  box.className = "badges";
  for (const text of tags.filter(Boolean)) {
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = text;
    box.append(badge);
  }
  return box;
}

function textSpan(className, text) {
  const span = document.createElement("span");
  span.className = className;
  span.textContent = text;
  return span;
}

// 對局玩家的一格：座位名稱、坐的人、標籤（房主、你、輪到…）、這個座位上的按鈕
function seatItem(label, who, tags, buttons = [], { empty = false, turn = false } = {}) {
  const li = document.createElement("li");
  li.classList.toggle("empty", empty);
  li.classList.toggle("turn", turn);
  li.append(textSpan("seat-name", label), textSpan("seat-who", who));
  if (tags.some(Boolean)) li.append(badgeList(tags));
  if (buttons.length) {
    const box = textSpan("seat-actions", "");
    box.append(...buttons);
    li.append(box);
  }
  return li;
}

// 房間成員的一列：名字、標籤、他在哪裡（黑棋、3 號位、觀戰…）、房主可以對他做的事
function memberItem(name, where, tags, buttons = []) {
  const li = document.createElement("li");
  li.append(textSpan("member-name", name));
  if (tags.some(Boolean)) li.append(badgeList(tags));
  li.append(textSpan("member-role", where));
  if (buttons.length) {
    const box = textSpan("member-actions", "");
    box.append(...buttons);
    li.append(box);
  }
  return li;
}

// 撲克開放牌桌（2026-11-16）：牌桌上的座位、隊列，上桌／排隊／觀眾／補 AI／結束牌局；房間成員分成桌上、排隊、觀戰席
function renderTablePlayers() {
  const started = state.startsAt !== null && !state.over;
  const lobby = inLobby();
  const host = state.owner && (lobby || started);
  const table = state.table || {};
  const leaving = new Set(table.leaving || []);
  const mine = myTableSeat();
  const ownerName = table.owner;
  const turn = started && roomStarted() ? state.frames.at(-1)?.acting : null;
  const seated = tableSeated();
  const total = Number(state.settings.players) || 0;
  const members = new Map(); // 名字 → 他在哪裡；依桌上、排隊、觀戰席的順序列出
  $("room-seats").replaceChildren(...seated.map((s) => {
    const name = s.token ? state.players[s.token] : null;
    if (name) members.set(name, `${s.seat} 號位`);
    const buttons = [];
    if (s.seat === mine && (lobby || started)) {
      buttons.push(lobby ? roomButton("下桌", () => tableAction("stand"))
        : roomButton(leaving.has(s.seat) ? "取消（繼續打）" : "觀眾", () => tableAction("stand")));
    }
    if (host && !s.token) {
      buttons.push(roomButton(leaving.has(s.seat) ? "取消（留在桌上）" : "請 AI 下桌",
        () => tableAction("remove_ai", { seat: s.seat }), leaving.has(s.seat) ? "" : "danger"));
    }
    const tags = [name && name === ownerName && "房主", s.seat === mine && "你", s.seat === turn && "輪到",
      leaving.has(s.seat) && "這手結束後下桌"];
    return seatItem(`${s.seat} 號位`, name ?? s.role, tags, buttons, { turn: s.seat === turn });
  }));
  if (!seated.length) $("room-seats").append(seatItem("牌桌", "還沒有人上桌", [], [], { empty: true }));
  $("room-seat-count").textContent = `${seated.length}／${total} 人`;
  // 牌桌下方：排隊的人與 AI，以及自己上桌／排隊、房主補 AI、結束牌局
  const box = $("room-table");
  box.replaceChildren();
  const queue = table.queue || [];
  if (started && queue.length) {
    const line = document.createElement("p");
    line.className = "table-queue";
    line.append(`排隊（${queue.length}）：`);
    queue.forEach((entry, i) => {
      const human = entry.token !== undefined;
      const name = human ? entry.name ?? "玩家" : aiLabel(entry);
      if (human && !members.has(name)) members.set(name, "排隊上桌");
      line.append(i ? "、" : "", name);
      if (!human && host) line.append(" ", roomButton("取消", () => tableAction("unqueue", { seat: i + 1 })));
    });
    box.append(line);
  }
  const actions = document.createElement("div");
  actions.className = "table-actions";
  const full = seated.length >= total;
  if (mine === null && (lobby || started)) {
    if (lobby) {
      const b = roomButton("上桌", () => tableAction("sit"), "primary");
      b.disabled ||= full;
      actions.append(b);
    } else {
      const inQueue = state.myToken !== null && queue.some((e) => e.token === state.myToken);
      actions.append(inQueue ? roomButton("離開隊列", () => tableAction("unqueue"))
        : roomButton("＋ 排隊上桌", () => tableAction("sit"), "primary"));
    }
  }
  if (host) {
    for (const [kind, text] of [["agent", "補 Agent AI"], ["bot", "補機器人"]]) {
      const b = roomButton(lobby ? text : `${text}（排隊）`, () => tableAction("add_ai", { kind }));
      b.disabled ||= lobby && full;
      actions.append(b);
    }
    if (started) actions.append(roomButton("結束牌局", endTable, "danger"));
  }
  if (actions.childElementCount) box.append(actions);
  box.hidden = !box.childElementCount;
  // 觀戰席：觀戰的人，加上上過桌、現在沒坐在桌上、還在房間裡的人
  const away = new Set(table.away || []);
  const watchers = [...state.spectators, ...Object.entries(state.players)
    .filter(([token, name]) => name && !away.has(Number(token))).map(([, name]) => name)];
  for (const name of watchers) if (!members.has(name)) members.set(name, "觀戰席");
  $("room-member-list").replaceChildren(...[...members].map(([name, where]) => memberItem(name, where,
    [name === ownerName && "房主", name === ME && "你"],
    lobby && host && name !== ownerName
      ? [roomButton("交給他當房主", () => transferHost(name)), roomButton("請離開", () => kickPlayer(name), "danger")]
      : [])));
  $("room-member-count").textContent = `${members.size} 人`;
}

async function endTable() {
  if (!window.confirm("結束這場牌局？不計勝負，所有人回到房間大廳，可以再開下一場。")) return;
  await roomAction("end_table", {}, "沒有結束牌局");
}

// 一般的真人對戰（每個座位一個人）：座位、空位、輪到誰；房間成員是坐著的人和觀戰的人
function renderSeatPlayers() {
  const turn = !state.over && roomStarted() && !waitingForPlayers() ? currentFrame()?.to_move : null;
  // 房間大廳（按開始之前，2026-11-15）：自己坐下或起身觀戰；房主還能幫人換座位、轉讓房主、請人離開。
  // 房主也可以觀戰（2026-11-18），觀戰中照樣管理房間；只有舊版撲克（CONFIG.pickHumanSeats）的房主固定 1 號位
  const lobby = inLobby();
  const host = lobby && state.owner;
  const fixedHost = CONFIG.pickHumanSeats;
  const mySeat = state.watching ? null : state.human;
  const seats = Object.entries(state.players).map(([key, name]) => [Number(key), name]);
  $("room-seats").replaceChildren(...seats.map(([seat, name]) => {
    const buttons = [];
    // 房主請 AI 坐的座位（2026-11-18 使用者要求「5個遊戲都可以讓ai進來」）：房主在大廳可以請 AI 離座
    if (state.aiSeats.includes(seat)) {
      const tags = [seat === turn && "輪到"];
      if (host && !fixedHost) buttons.push(roomButton("請 AI 離座", () => setAiSeat(seat, false), "danger"));
      return seatItem(seatName(seat), "AI", tags, buttons, { turn: seat === turn });
    }
    if (lobby && name === null && !(host && fixedHost)) buttons.push(roomButton("坐這裡", () => moveTo("", seat)));
    if (host && !fixedHost && name === null) buttons.push(roomButton("請 AI 坐這裡", () => setAiSeat(seat, true)));
    if (lobby && seat === mySeat && !(state.owner && fixedHost)) buttons.push(roomButton("改成觀戰", () => moveTo("", 0)));
    const tags = [seat === state.hostSeat && "房主", seat === mySeat && "你", seat === turn && "輪到"];
    return seatItem(seatName(seat), name ?? "等待加入…", tags, buttons, { empty: name === null, turn: seat === turn });
  }));
  const taken = seats.filter(([, name]) => name !== null);
  $("room-seat-count").textContent = `${taken.length + state.aiSeats.length}／${seats.length} 人`;
  // 舊版撲克（固定座位）的其他座位由 AI 坐
  const aiSeats = (Number(state.settings.players) || 0) - seats.length;
  $("room-table").textContent = aiSeats > 0 ? `另外 ${aiSeats} 個座位由 AI 坐。` : "";
  $("room-table").hidden = aiSeats <= 0;
  const rows = taken.map(([seat, name]) => memberItem(name, seatName(seat),
    [seat === state.hostSeat && "房主", seat === mySeat && "你"],
    !host ? [] : seat === mySeat
      ? (fixedHost ? [] : [seatPicker("", seat)])
      : [seatPicker(name, seat), roomButton("交給他當房主", () => transferHost(name)),
        roomButton("請離開", () => kickPlayer(name), "danger")]));
  for (const name of state.spectators) {
    const me = name === ME;
    rows.push(memberItem(name, "觀戰", [name === state.ownerName && "房主", me && "你"],
      !host ? [] : me ? [seatPicker("", null)]
        : [seatPicker(name, null), ...(fixedHost ? [] : [roomButton("交給他當房主", () => transferHost(name))]),
          roomButton("請離開", () => kickPlayer(name), "danger")]));
  }
  $("room-member-list").replaceChildren(...rows);
  $("room-member-count").textContent = `${rows.length} 人`;
}

function renderRoomPlayers() {
  const show = state.pvp && state.gameId !== null;
  $("room-players").hidden = !show;
  $("room-members").hidden = !show;
  if (!show) return;
  if (isTable()) renderTablePlayers();
  else renderSeatPlayers();
  // 房主在觀戰中也能關閉房間（撲克開放牌桌的觀戰席；2026-11-18 起每種遊戲都可以觀戰）
  $("close-room").hidden = !(state.owner && state.startsAt === null && !state.over);
  $("close-room").disabled = state.busy || state.starting;
  $("room-footer").hidden = $("close-room").hidden && $("leave-room").hidden;
  renderRoomLock();
}

// ---- 房間鎖（2026-11-18 使用者要求「房間預設公開 房主可自行設定房間是否上鎖 密碼可以自己設定或是隨機」）----
// 每個人都看得到房間是公開還是上鎖；只有房主看得到密碼、能上鎖、改密碼、解鎖。密碼空白時伺服器隨機產生
function renderRoomLock() {
  const box = $("room-lock");
  box.hidden = !state.pvp || state.gameId === null || state.closed || state.over;
  if (box.hidden) return;
  $("room-lock-state").textContent = !state.locked
    ? "公開：房間列表上的人都能加入或觀戰"
    : state.owner && state.roomPassword
      ? `🔒 上鎖，房間密碼 ${state.roomPassword}（不在房間裡的人要輸入才能加入或觀戰）`
      : "🔒 上鎖：不在房間裡的人要輸入房間密碼才能加入或觀戰";
  $("room-lock-form").hidden = !state.owner;
  $("room-lock-apply").textContent = state.locked ? "改密碼" : "上鎖";
  $("room-unlock").hidden = !state.locked;
  for (const id of ["room-lock-apply", "room-unlock", "room-lock-password"]) $(id).disabled = state.busy;
}

async function setRoomLock(lock) {
  const password = lock ? $("room-lock-password").value.trim() : "";
  state.busy = true;
  updateButtons();
  try {
    const data = await api(`games/${state.gameId}/lock/`, { lock, password });
    state.locked = Boolean(data.game.locked);
    state.roomPassword = data.room_password ?? "";
    $("room-lock-password").value = "";
    setStatus(lock ? `房間已上鎖，房間密碼是 ${state.roomPassword}。把房間編號和密碼一起傳給朋友。` : "房間已解鎖，房間列表上的人都能加入或觀戰。");
  } catch (err) {
    setStatus(`房間鎖沒有更新：${err.message}`);
  } finally {
    state.busy = false;
    updateButtons();
  }
}

// 被房主請出房間（即時連線收到 left，2026-11-15）：不能再下、不能再聊天；可以再用房間密碼回來
function leftRoom() {
  Object.assign(state, { closed: true, over: true });
  closeSocket();
  stopCountdown();
  showInvite("");
  rememberRoom();
  setStatus("房主請你離開了這個房間。想回來的話，可以再用房間編號加入。");
  updateButtons();
}

// ---- 房間大廳（2026-11-15 使用者要求「房主可以自由選擇」）：按開始之前換座位、請人離開、轉讓房主、離開房間 ----
// 結果由伺服器推送回來（每個人照自己現在的座位重畫），這裡只送出請求

async function roomAction(path, body, failText) {
  state.busy = true;
  updateButtons();
  try {
    await api(`games/${state.gameId}/${path}/`, body);
  } catch (err) {
    setStatus(`${failText}：${err.message}`);
  } finally {
    state.busy = false;
    updateButtons();
  }
  await flushPendingUpdate();
}

// player 空的＝自己；seat 0＝改成觀戰
const moveTo = (player, seat) => roomAction("seat", { player, seat }, "沒有換成座位");
// 房主請 AI 坐這個座位或離座（2026-11-18）
const setAiSeat = (seat, on) => roomAction("seat_ai", { seat, on }, on ? "AI 沒有入座" : "AI 沒有離座");

function kickPlayer(name) {
  if (window.confirm(`請 ${name} 離開房間？他之後還可以用房間編號回來。`)) roomAction("kick", { player: name }, "沒有請他離開");
}

function transferHost(name) {
  if (window.confirm(`把房主交給 ${name}？之後改設定、按「開始」都由他決定。`)) {
    roomAction("host", { player: name }, "沒有轉讓房主");
  }
}

async function leaveRoom() {
  const text = state.owner
    ? "離開房間？房主會交給房間裡的下一個人（先找坐在座位上的人，再找觀戰的人）；房間裡沒有別人才關閉房間。"
    : "離開房間？之後可以再用房間編號回來。";
  if (!window.confirm(text)) return;
  state.busy = true;
  updateButtons();
  try {
    await api(`games/${state.gameId}/leave/`, {});
    closeSocket();
    window.location.href = "/";
  } catch (err) {
    setStatus(`無法離開房間：${err.message}`);
    state.busy = false;
    updateButtons();
  }
}

// 房主換遊戲：同一個房間（同一組密碼）改成別的遊戲，到那個遊戲的頁面繼續（其他人由推送跟著換頁）
async function switchGame() {
  const target = CONFIG.games.find((g) => g.key === $("room-game").value);
  const table = !canChangeRoom() && canSwitchTable(); // 撲克牌局進行中：先結束牌局
  if (!target || target.key === CONFIG.key || !(canChangeRoom() || table)) return;
  if (table && !window.confirm(`結束這場牌局並換成${target.name}？不計勝負，桌上的人回到房間大廳。`)) {
    $("room-game").value = CONFIG.key;
    return;
  }
  // 撲克要選哪些座位是真人：預設目前坐著的人數（至少 2 位），進去後還能在設定改
  const seated = Object.values(state.players).filter(Boolean).length;
  const settings = target.pickSeats ? { humans: Array.from({ length: Math.max(2, seated) }, (_, i) => i + 1) } : {};
  state.busy = true;
  updateButtons();
  setStatus(`換成${target.name}中…`);
  let gameId = state.gameId;
  try {
    if (table) {
      closeSocket(); // 結束牌局的推送會帶著頁面回到大廳；這裡自己接著換遊戲
      gameId = (await api(`games/${gameId}/end_table/`, {})).game.next_round ?? gameId;
    }
    // 房主留在原本的座位號碼（2026-11-22 使用者決定；原本寫死 1 號位，坐白棋的房主換遊戲後和對方對調先後手）。
    // 撲克牌桌上的座位和棋類的不是同一回事，從牌桌換過來、或新遊戲沒有這個座位時才坐 1 號位
    const keep = !table && !isTable() && !state.watching && target.seats.includes(state.human);
    const data = await api(`games/${gameId}/room/`, { game_type: target.key, seat: keep ? state.human : 1, settings });
    closeSocket();
    window.location.href = `/${target.key}/?resume=${data.game.id}`;
  } catch (err) {
    if (gameId !== state.gameId) { // 牌局已經結束、遊戲沒換成：回到房間大廳（新的那一盤），在那裡再換一次
      state.busy = false;
      await openGame(gameId, "resume");
      setStatus(`牌局已經結束，但沒有換成${target.name}：${err.message}`);
      return;
    }
    if (table) openSocket(state.gameId); // 沒有結束牌局：接回即時同步
    setStatus(`無法換遊戲：${err.message}`);
    $("room-game").value = CONFIG.key;
    state.busy = false;
    updateButtons();
  }
}

// ---- 房主關閉房間（2026-11-13 使用者要求）：只在按開始之前；之後要結束請認輸 ----

// 房間關閉了（自己關的、或收到房主關閉的推送、或打開一個已關閉的房間）：不能再下、不能再聊天
function roomClosed() {
  Object.assign(state, { closed: true, over: true });
  closeSocket();
  stopCountdown();
  showInvite("");
  rememberRoom();
  setStatus(state.owner ? "房間已關閉。可以按「新對局」，或回首頁再開一間。" : "房主已經關閉這個房間。");
  updateButtons();
}

async function closeRoom() {
  if (!window.confirm("關閉這個房間？朋友就不能再用房間編號加入或觀戰，這個房間也會從清單上拿掉。")) return;
  state.busy = true;
  updateButtons();
  try {
    await api(`games/${state.gameId}/close/`, {});
    pendingUpdate = null;
    roomClosed();
  } catch (err) {
    setStatus(`無法關閉房間：${err.message}`);
  } finally {
    state.busy = false;
    updateButtons();
  }
}

// 在真人對戰的房間裡時，網址留著房間編號（2026-11-13 使用者回報回不到自己開的房間）：重新整理、上一頁都回到同一個房間。
// 和 AI 對戰時網址維持 /<遊戲>/，重新整理照舊開新局
function rememberRoom() {
  const query = state.pvp && state.gameId !== null && !state.closed ? `?${state.watching ? "watch" : "resume"}=${state.gameId}` : "";
  window.history.replaceState(null, "", window.location.pathname + query);
  // 頂端的「回到房間」（_room_banner.html）指的就是正在看的這間時收起來
  const banner = $("room-banner");
  if (banner) banner.hidden = state.gameId !== null && Number(banner.dataset.game) === state.gameId;
}

// 真人對戰一開房間就要存檔（要有房間密碼給朋友），不像和 AI 下時等第一步才建立
async function newPvpGame(settings) {
  state.busy = true;
  updateButtons();
  setStatus("開房間中…");
  try {
    const data = await api("games/", { seat: state.human, settings, mode: "pvp", ...agentOptions() });
    if (data.game.settings.table) { // 撲克開放牌桌：開房的人先在觀戰席（2026-11-16）
      await openGame(data.game.id, "resume");
      return;
    }
    state.gameId = data.game.id;
    state.owner = true;
    rememberRoom();
    setRoomPeople(data.game);
    state.frames = [data.frame];
    showInvite(data.game.room_code);
    openSocket(data.game.id);
    draw();
    statusAfterMove(data.game);
  } catch (err) {
    setStatus(`無法開房間：${err.message}`);
  } finally {
    state.busy = false;
    updateButtons();
  }
}

// 房主在開始前改設定（2026-11-08；2026-11-15 起朋友坐下後也能改）：套用到同一個房間（房間密碼不變），大家跟著換
// 房間大廳：真人對戰按「開始」之前（2026-11-15 起朋友坐下後房主也能改設定、換遊戲）
const inLobby = () => state.pvp && state.gameId !== null && state.startsAt === null && !state.over && !state.closed;
// 房主在觀戰中也能改設定（撲克開放牌桌的觀戰席；2026-11-18 起每種遊戲都可以觀戰）
const canChangeRoom = () => inLobby() && state.owner;
// 撲克開放牌桌開始後（2026-11-20 使用者回報「為什麼房間在德州撲克時不能切遊戲」，使用者選擇放寬）：牌桌沒有終局，
// 房主換遊戲＝先「結束牌局」（不計勝負）再在回到的大廳換。只有撲克：棋類開始後不能換，快輸的一方才不能換遊戲逃掉
const canSwitchTable = () => isTable() && state.owner && state.gameId !== null && state.startsAt !== null
  && !state.over && !state.closed && !state.reviewing;
// 在房間裡但不能改設定（不是房主、或已經開始）：設定面板顯示房間的設定並鎖住，不是和 AI 對戰的表單
const roomSettingsLocked = () => state.pvp && state.gameId !== null && !state.closed && !state.reviewing
  && !canChangeRoom();

async function changeRoom() {
  if (!pvpChosen()) {
    setStatus("要和 AI 下的話按「新對局」；這個房間會留著，朋友仍然可以用房間編號加入。");
    return;
  }
  const settings = readSettings();
  const seat = Number($("human-seat").value);
  state.busy = true;
  updateButtons();
  setStatus("更新房間設定中…");
  try {
    const data = await api(`games/${state.gameId}/room/`, { seat, settings });
    const gameId = state.gameId;
    // 撲克開放牌桌（還坐得下的人照樣坐著）、觀戰中的房主（2026-11-18，改完照樣觀戰）：重新打開這個房間
    if (data.game.settings.table || data.my_seat === null) {
      await openGame(gameId, "resume");
      setStatus(`${data.game.settings.table ? "牌桌" : "房間"}設定已更新。按「開始」之前都還能改；房間編號不變。`);
      return;
    }
    resetView(seat, data.game.settings, [], [data.frame], true, data.game.players);
    setRoomPeople(data.game);
    Object.assign(state, { gameId, owner: true });
    pendingUpdate = null; // 改設定之前收到的推送是舊設定的棋盤，不能再套用
    showInvite(data.game.room_code);
    openSocket(gameId);
    draw();
    setStatus("房間設定已更新。按「開始」之前都還能改；房間編號不變。");
  } catch (err) {
    setStatus(`無法更新房間設定：${err.message}`);
  } finally {
    state.busy = false;
    updateButtons();
  }
}

// ---- 房主按「開始」與倒數（使用者要求，2026-11-13）----

// 伺服器給的是「還剩幾秒」（不是時間點），換成這台電腦的時鐘，兩邊的時鐘不準也沒關係。
// 按過開始之後時間點不再變（每次推送都重算的話會因為傳送延遲一直往後跳）
function setStartsIn(seconds) {
  if (seconds === null || seconds === undefined || state.startsAt !== null) return;
  state.startsAt = Date.now() + seconds * 1000;
  if (seconds > 0) runCountdown();
}

let countdownTimer = null;

function stopCountdown() {
  clearInterval(countdownTimer);
  countdownTimer = null;
  $("countdown").hidden = true;
}

function runCountdown() {
  stopCountdown();
  const round = state.round;
  const tick = () => {
    const left = Math.ceil((state.startsAt - Date.now()) / 1000);
    if (round !== state.round) return stopCountdown();
    if (left > 0) {
      $("countdown").hidden = false;
      $("countdown").textContent = String(left);
      return;
    }
    stopCountdown();
    statusAfterMove({});
    updateButtons();
  };
  tick();
  statusAfterMove({});
  updateButtons();
  countdownTimer = setInterval(tick, 100);
}

async function startRoom() {
  state.starting = true;
  updateButtons();
  try {
    // 倒數由伺服器的推送開始；請求要等倒數完（AI 的座位倒數後才行動）才回來
    const data = await api(`games/${state.gameId}/start/`, agentOptions());
    setStartsIn(data.game.starts_in); // 推送沒收到（例如即時連線斷了）時，至少知道已經開始
  } catch (err) {
    setStatus(`無法開始：${err.message}`);
  } finally {
    state.starting = false;
    updateButtons();
  }
}

// ---- 房間聊天室（使用者要求，2026-11-13）----
// 房主按開始前，玩家和觀戰的人在同一個聊天室；按了之後觀戰的人換到觀戰者專屬的聊天室，兩邊互相看不到

const chatOpen = () => !$("chat").hidden;

function setChatOpen(open) {
  $("chat").hidden = !open;
  $("chat-toggle").hidden = open || !(state.pvp && state.gameId !== null);
  $("chat-toggle").setAttribute("aria-expanded", String(open));
  if (open) {
    markChatSeen();
    $("chat-log").scrollTop = $("chat-log").scrollHeight;
    $("chat-input").focus();
  }
}

function markChatSeen() {
  state.chatSeen = state.chat.at(-1)?.id || 0;
  $("chat-badge").hidden = true;
}

function renderChat(messages) {
  state.chat = messages;
  const log = $("chat-log");
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  const started = state.startsAt !== null;
  log.replaceChildren(...messages.map((m) => {
    const li = document.createElement("li");
    li.classList.toggle("mine", m.mine);
    const meta = document.createElement("span");
    meta.className = "chat-meta";
    const time = new Date(m.at).toLocaleTimeString("zh-TW", { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
    meta.textContent = `${m.mine ? "你" : m.name}・${time}${started && m.room === "lobby" ? "・開始前" : ""}`;
    li.append(meta, m.text);
    return li;
  }));
  if (!messages.length) {
    const li = document.createElement("li");
    li.className = "chat-empty";
    li.textContent = "還沒有人說話。";
    log.append(li);
  }
  if (atBottom || messages.at(-1)?.mine) log.scrollTop = log.scrollHeight;
  if (chatOpen()) markChatSeen();
  else {
    const unread = messages.filter((m) => m.id > state.chatSeen && !m.mine).length;
    $("chat-badge").hidden = !unread;
    $("chat-badge").textContent = String(unread);
  }
  updateChatAvailability();
}

function updateChatAvailability() {
  const room = state.pvp && state.gameId !== null;
  if (!room) {
    $("chat").hidden = true;
    $("chat-toggle").hidden = true;
  } else if ($("chat").hidden) $("chat-toggle").hidden = false;
  const started = state.startsAt !== null;
  $("chat-title").textContent = !started ? "聊天室" : state.watching ? "聊天室（觀戰者）" : "聊天室（玩家）";
  $("chat-note").textContent = !started ? "開始前，房間裡的每個人（包括觀戰的人）都在這裡聊天。"
    : state.watching ? "對局開始後，觀戰的人在這個專屬聊天室聊天，玩家看不到。"
      : "對局開始後只有玩家在這裡，觀戰的人在另一個聊天室。";
  const closed = state.over;
  $("chat-input").disabled = closed || state.chatSending;
  $("chat-input").placeholder = closed ? "對局已經結束，只能看" : "說點什麼…";
  $("chat-form").querySelector("button").disabled = closed || state.chatSending;
}

async function sendChat(event) {
  event.preventDefault();
  const text = $("chat-input").value.trim();
  if (!text || state.chatSending) return;
  state.chatSending = true;
  updateChatAvailability();
  $("chat-note").textContent = "送出中…";
  let error = "";
  try {
    const message = await api(`games/${state.gameId}/chat/`, { text });
    $("chat-input").value = "";
    if (!state.chat.some((m) => m.id === message.id)) renderChat([...state.chat, message]);
  } catch (err) {
    error = `沒有送出：${err.message}`;
  }
  state.chatSending = false;
  updateChatAvailability();
  if (error) $("chat-note").textContent = error;
  if (!$("chat-input").disabled) $("chat-input").focus();
}

// 設定面板跟著伺服器的房間設定（另一個分頁改過、或重新打開房間時）
function syncSettingsForm(seat, settings) {
  $("opponent").value = "pvp";
  // 撲克開放牌桌的 seat 是成員代號，不是選單裡的座位：只套用選單裡有的
  if (seat && $("human-seat").querySelector(`option[value="${seat}"]`)) $("human-seat").value = String(seat);
  document.querySelectorAll(".game-setting").forEach((el) => {
    if (settings[el.dataset.name] !== undefined) el.value = String(settings[el.dataset.name]);
    // 只記非預設值的設定（撲克的勝率表只記禁用）：沒有就回到預設
    else if (el.closest("[data-pvp-only]")) el.value = el.querySelector("option[selected]")?.value ?? el.value;
  });
  updateOpponentFields();
}

// 首頁的「建立對戰房間」（?room=new）：先開好房間，再打開設定讓房主調整
async function createRoom() {
  $("opponent").value = "pvp";
  updateOpponentFields();
  await newGame();
  if (state.gameId !== null) setPopover("settings-panel", true);
}

// 玩家先下時等第一步才建立存檔，避免重新整理頁面就多出空的對局
async function newGame() {
  closePopovers();
  const settings = readSettings();
  resetView(Number($("human-seat").value), settings, [], [GameBoard.emptyFrame(settings)], pvpChosen());
  state.gameId = null;
  rememberRoom(); // 離開原本的房間：網址不再指向它
  renderChat([]);
  updateButtons();
  if (state.pvp) {
    await newPvpGame(settings);
    return;
  }
  if (humanTurn()) {
    yourTurnStatus();
    return;
  }
  state.busy = true;
  updateButtons();
  setThinkingStatus();
  try {
    const data = await api("games/", { seat: state.human, settings, ...agentOptions() });
    state.gameId = data.game.id;
    state.frames = [data.frame];
    await applyAiMoves(data.ai_moves);
    statusAfterMove(data.game);
  } catch (err) {
    setStatus(`無法建立對局：${err.message}`);
  } finally {
    state.busy = false;
    updateButtons();
  }
}

async function playAction(action) {
  state.busy = true;
  state.hover = null;
  state.pending = action; // 伺服器回應前先畫出玩家這一手，不必等 Agent 想完才看到
  draw();
  updateButtons();
  if (state.pvp) setStatus("送出中…");
  else setThinkingStatus();
  try {
    if (state.gameId === null) {
      state.gameId = (await api("games/", { seat: state.human, settings: state.settings, ...agentOptions() })).game.id;
    }
    const data = await api(`games/${state.gameId}/move/`, { action, ...agentOptions() });
    state.pending = null;
    state.actions.push(data.action);
    state.frames.push(data.frame);
    draw();
    await applyAiMoves(data.ai_moves);
    // 撲克：這次回來的思路都沒被遮住，代表那手牌結束了，之前遮住的思路可以完整顯示
    if (state.hiddenNotes && !data.ai_moves.some((m) => m.hidden)) await reloadNotes();
    statusAfterMove(data);
  } catch (err) {
    state.pending = null;
    draw();
    setStatus(`發生錯誤：${err.message}。請再下一次。`);
  } finally {
    state.busy = false;
    updateButtons();
  }
  await flushPendingUpdate();
}

// 「結束（不計勝負）」：對局停在這裡、不算輸贏、不進戰績，之後仍可復盤（使用者要求，2026-10-21）
async function endGame() {
  if (state.busy || state.gameId === null || state.over) return;
  if (!window.confirm("結束這盤、不計勝負？之後不能再接著下，但還可以復盤。")) return;
  state.busy = true;
  updateButtons();
  try {
    await api(`games/${state.gameId}/end/`, {});
    state.over = true;
    setStatus("這盤已結束，不計勝負。可以按「復盤」檢討，或按「新對局」再下一盤。");
  } catch (err) {
    setStatus(`無法結束對局：${err.message}`);
  } finally {
    state.busy = false;
    updateButtons();
  }
}

canvas.addEventListener("mousemove", (event) => {
  const name = !state.busy && humanTurn() ? GameBoard.pointAt(canvas, event) : null;
  const cell = name && GameBoard.canPlay(currentFrame(), name) ? name : null;
  if (cell !== state.hover) {
    state.hover = cell;
    draw();
  }
});

canvas.addEventListener("mouseleave", () => {
  if (state.hover) {
    state.hover = null;
    draw();
  }
});

canvas.addEventListener("click", (event) => {
  if (state.busy || !humanTurn()) return;
  const name = GameBoard.pointAt(canvas, event);
  if (GameBoard.select) {
    // 要點兩下的遊戲（象棋：先選棋子再點目的地）：選好目的地時才回傳一步
    const action = GameBoard.select(currentFrame(), name);
    draw();
    if (action) playAction(action);
    else yourTurnStatus();  // 點了沒有合法步的棋子時，turnHint 會說明為什麼不能走
  } else if (name && GameBoard.canPlay(currentFrame(), name)) {
    playAction(name);
  }
});

// ---- 說明 ----

// 證據種類：資料裡是研究用的名稱（規則確認…），畫面上給玩家看白話的名稱，滑過時有說明
const EVIDENCE = {
  規則確認: ["依規則確認", "照遊戲規則算出來的，一定正確"],
  求解確認: ["算到底確認", "電腦把變化算到底得到的結果"],
  引擎估計: ["電腦估計", "電腦的評估，不一定準"],
  條件式計算: ["依假設計算", "照列出的假設算的（例如猜對手的牌）"],
};
const evidenceLabel = (evidence) => EVIDENCE[evidence]?.[0] ?? evidence;

function evidenceTags(facts) {
  return [...new Set(facts.points.map((p) => p.evidence))].map((evidence) => {
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.dataset.evidence = evidence; // 樣式依證據種類上色
    tag.textContent = evidenceLabel(evidence);
    tag.title = EVIDENCE[evidence]?.[1] ?? "";
    return tag;
  });
}

function setSpot(key, value) {
  state[key] = value;
  draw();
}

// 把文字中的座標換成可以指的標籤：滑過暫時標示，點一下固定標示（再點一次取消）
function renderRichText(el, text) {
  el.replaceChildren();
  let last = 0;
  for (const match of text.matchAll(COORD_RE)) {
    const name = match[1];
    const start = match.index + match[0].indexOf(name);
    el.append(text.slice(last, start));
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "coord";
    chip.textContent = name;
    chip.title = `在棋盤上標示 ${name}`;
    chip.addEventListener("mouseenter", () => setSpot("hoverHighlight", name));
    chip.addEventListener("mouseleave", () => setSpot("hoverHighlight", null));
    chip.addEventListener("click", (event) => {
      event.stopPropagation(); // 只標示座標，不觸發整則說明的跳轉
      setSpot("highlight", state.highlight === name ? null : name);
    });
    el.append(chip);
    last = start + name.length;
  }
  el.append(text.slice(last));
}

function entry(headText, facts, placeholder = "說明產生中…") {
  const li = document.createElement("li");
  const head = document.createElement("div");
  head.className = "head";
  head.append(headText, " ", ...evidenceTags(facts));
  const key = document.createElement("p");
  key.className = "key";
  key.hidden = true;
  const text = document.createElement("p");
  text.className = "text";
  text.textContent = placeholder;
  const source = document.createElement("div");
  source.className = "source";
  // 語言模型可能講錯關係，所以原始事實一律可以展開對照
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = "查看電腦分析原文";
  summary.addEventListener("click", (event) => event.stopPropagation());
  const list = document.createElement("ul");
  for (const p of facts.points) {
    const item = document.createElement("li");
    renderRichText(item, `［${evidenceLabel(p.evidence)}］${p.text}`);
    list.append(item);
  }
  details.append(summary, list);
  li.append(head, key, text, source, details);
  return li;
}

// 伺服器存的紀錄是給研究用的（含連線錯誤訊息、檢查失敗的原因），畫面上換成白話；原文仍可在「查看電腦分析原文」展開
const TAKEOVER_RE = /Agent 沒有完成這一手（[\s\S]*?），改由引擎代下。/g;
// 事實原文保留研究用的「依引擎估計」等寫法（伺服器與「查看電腦分析原文」不變），說明與問題標題裡換成局面上的說法。
// 2026-11-27 使用者：「AI 後期的下棋和問題檢查都說依照引擎，看起來只是在抄引擎給的思路」
const PLAIN_WORDS = [
  [TAKEOVER_RE, "AI 這一步沒有完成查證，改由電腦直接下。"],
  [/依引擎估計(?:（[^）]*）)?[，,]?/g, "大致來看，"],
  [/引擎候選（依評估由好到壞）/g, "候選點（由好到壞）"],
  [/看引擎候選/g, "看候選點"],
  [/建議：引擎推薦 /g, "建議：可以改下 "],
  [/引擎(?:把|沒有在)/g, (m) => m.replace("引擎", "問題檢查")],
  [/引擎的(?:最佳)?選擇/g, "最好的選擇"],
  [/引擎會?(?=(?:比較|更)?(?:推薦|建議))/g, ""],
  [/引擎/g, "電腦"],
];
const plainFacts = (text) => PLAIN_WORDS.reduce((t, [re, to]) => t.replace(re, to), text);

function plainNote(note) {
  if (note.startsWith("Ollama 無法使用")) return "AI 說明暫時連不上";
  if (note.startsWith("模型提到事實中沒有的座標")) return "AI 的說明提到了分析裡沒有的位置，所以不採用";
  if (note.startsWith("只有引擎估計或條件式計算")) return "AI 的說明講得比分析更肯定，所以不採用";
  if (note.startsWith("沒有求解確認的事實")) return "AI 的說明講得比分析更肯定，所以不採用";
  if (note.startsWith("模型提到了「引擎」")) return "AI 的說明沒有講出局面上的理由，所以不採用";
  return note;
}

function showResult(li, result, llmSource) {
  const key = li.querySelector(".key");
  key.hidden = !result.key;
  if (result.key) renderRichText(key, result.key);
  renderRichText(li.querySelector(".text"), plainFacts(result.text));
  // 模型成功時（llm、agent）結果沒有 note，只有其他情況才拿 note 來顯示
  let source;
  if (result.source === "llm") source = llmSource;
  else if (result.source === "agent") source = `AI 查證後決定，花了 ${result.seconds} 秒`;
  else if (["hidden", "plain"].includes(result.source)) source = result.note;
  else source = `${plainNote(result.note)}，先顯示電腦分析原文`;
  li.querySelector(".source").textContent = source;
}

const factsText = (facts) => facts.points.map((p) => p.text).join("");

function queueNarration(li, path, body, facts, llmSource) {
  const round = state.round;
  li.querySelector(".text").textContent = "說明產生中…";
  narrationQueue = narrationQueue.then(async () => {
    if (round !== state.round) return; // 已換到別盤棋，略過舊的說明
    try {
      showResult(li, await api(path, { ...body, model: $("model").value }), llmSource);
    } catch (err) {
      showResult(li, { text: factsText(facts), note: err.message }, "");
    }
  });
}

// AI 的一步思路。Agent 下的棋落子時就附上它的查證過程；引擎代下或不需查證的一步沒有說明，
// live（剛下的）立即請模型說明，打開舊局時則用按鈕觸發
function addNote(note, live) {
  const action = state.actions[note.index];
  const takeover = note.facts.points[0]?.text.includes("改由引擎代下");
  const head = `第 ${note.index + 1} ${STEP}　${seatName(note.seat)}（AI）${actionText(action)}${takeover ? "（由電腦代下）" : ""}`;
  const li = entry(head, note.facts);
  const path = `games/${state.gameId}/notes/${note.index}/narrate/`;
  if (note.hidden) {
    state.hiddenNotes = true;
    showResult(li, { text: factsText(note.facts), note: "完整思路含底牌，這手牌結束後復盤時可以看", source: "hidden" }, "");
  } else if (note.narration) {
    showResult(li, note.narration, "AI 的白話說明");
  } else if (live && !state.watching && !coachLocked()) {
    queueNarration(li, path, {}, note.facts, "AI 的白話說明");
  } else if (state.watching || coachLocked()) {
    // 觀戰的人不能請 AI 說明；真人對戰結束前伺服器也會拒絕（operations.coach_allowed），不送出請求
    showResult(li, { text: factsText(note.facts), note: state.watching ? "" : "真人對戰結束後可以請 AI 說明" }, "");
  } else {
    showResult(li, { text: factsText(note.facts), note: "這一手還沒有 AI 說明" }, "");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "narrate-button";
    button.textContent = "請 AI 說明";
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      button.remove();
      queueNarration(li, path, {}, note.facts, "AI 的白話說明");
    });
    li.querySelector(".source").after(button);
  }
  li.addEventListener("click", () => {
    if (!state.reviewing) return;
    revealBoard();
    goTo(note.index + 1);
    li.classList.add("active");
    setSpot("highlight", action);
  });
  $("thoughts-empty").hidden = true;
  $("ai-log").prepend(li);
  if (live && $("coach").hidden) setUnread(state.unread + 1);
}

// ---- 復盤 ----

async function startReview() {
  if (coachLocked()) return;
  if (!state.over && !window.confirm("對局還沒結束，要先復盤嗎？之後可以從首頁接著下。")) return;
  closePopovers();
  Object.assign(state, { over: true, reviewing: true, busy: true, viewAt: state.actions.length, marks: null });
  $("review-nav").hidden = false;
  $("review-summary").textContent = "復盤分析中…";
  $("moments").replaceChildren();
  $("ai-log").classList.add("clickable");
  setCoachOpen(true);
  selectTab("moments");
  updateButtons();
  updateNav();
  draw();
  setStatus("復盤分析中…");
  try {
    if (state.hiddenNotes) await reloadNotes();
    const data = await api(`games/${state.gameId}/review/`);
    renderMoments(data.moments, data.overall);
    renderOpponents(data.opponents || []);
    setStatus("復盤中：用 ◀ ▶ 一步一步回看；AI 教練裡有思路、問題檢查，也可以發問。");
  } catch (err) {
    $("review-summary").textContent = "";
    setStatus(`復盤失敗：${err.message}`);
  } finally {
    state.busy = false;
    updateButtons();
  }
}

// 對局中被遮住的思路（撲克的底牌），復盤時向伺服器重新拿；這手牌已結束的會完整顯示
async function reloadNotes() {
  const g = await api(`games/${state.gameId}/`);
  state.hiddenNotes = false;
  $("ai-log").replaceChildren();
  for (const note of g.notes) addNote(note, false);
}

// 問題檢查分成四類：關鍵時刻（影響最大）、小失誤（影響較小）、建議（不算失誤，是更推薦的下法）、做得好（例如撲克的精準止損）
const MOMENT_GROUPS = [
  { level: "key", title: "關鍵時刻", hint: "影響最大的決定，先看這幾則。" },
  { level: "minor", title: "小失誤", hint: "一次的影響比較小，但同樣的習慣累積起來也會吃虧。" },
  { level: "suggest", title: "建議", hint: "不算失誤，是更推薦的下法，可以參考。" },
  { level: "good", title: "做得好", hint: "判斷正確、值得保持的地方。" },
];

function renderMoments(moments, overall) {
  const count = (level) => moments.filter((m) => (m.level || "key") === level).length;
  const [key, minor, suggest, good] = MOMENT_GROUPS.map((g) => count(g.level));
  const extras = [suggest ? `${suggest} 個建議` : "", good ? `${good} 個做得好的地方` : ""].filter(Boolean);
  const extraText = extras.length ? `；另外有 ${extras.join("、")}` : "";
  $("review-summary").textContent = key + minor
    ? `找到 ${key} 個關鍵時刻、${minor} 個小失誤${extraText}。點一則可以看當時的局面。`
    : `沒有找到問題${extraText}。`;
  // 整體評論放最上面，最先請 AI 寫
  if (overall) {
    const section = document.createElement("section");
    section.className = "moment-group overall";
    const heading = document.createElement("h3");
    heading.textContent = "整體評論";
    const list = document.createElement("ol");
    list.className = "entries";
    const li = entry("這盤整體表現", overall);
    list.append(li);
    section.append(heading, list);
    $("moments").append(section);
    queueNarration(li, "narrate/", { facts: overall }, overall, "AI 依檢查結果整理");
  }
  for (const group of MOMENT_GROUPS) {
    const items = moments.filter((m) => (m.level || "key") === group.level);
    if (!items.length) continue;
    const section = document.createElement("section");
    section.className = `moment-group ${group.level}`;
    const heading = document.createElement("h3");
    heading.textContent = `${group.title}（${items.length}）`;
    const hint = document.createElement("p");
    hint.className = "muted";
    hint.textContent = group.hint;
    const list = document.createElement("ol");
    list.className = "entries moments";
    section.append(heading, hint, list);
    $("moments").append(section);
    for (const m of items) {
      const li = entry(`第 ${m.index + 1} ${STEP} ${actionText(m.played)}：${plainFacts(m.title)}`, m.facts);
      li.addEventListener("click", () => showMoment(m, li));
      list.append(li);
      // 關鍵時刻自動請 AI 說明；其他的數量可能很多，按了再請，免得排隊等很久
      if (group.level === "key") {
        queueNarration(li, "narrate/", { facts: m.facts }, m.facts, "AI 的白話說明");
      } else {
        showResult(li, { text: factsText(m.facts), note: "想看白話說明可以按「請 AI 說明」", source: "plain" }, "");
        const button = document.createElement("button");
        button.type = "button";
        button.className = "narrate-button";
        button.textContent = "請 AI 說明";
        button.addEventListener("click", (event) => {
          event.stopPropagation();
          button.remove();
          queueNarration(li, "narrate/", { facts: m.facts }, m.facts, "AI 的白話說明");
        });
        li.querySelector(".source").after(button);
      }
    }
  }
}

// 對手觀察（真人對戰結束後，2026-11-05）：對手的傾向、出現跡象的步數、應對建議；放在整體評論下面。
// 每位對手一則（AI 依事實改寫），下面列出電腦在他身上找到的每一步，點了跳到那一步並標出他下的和電腦建議的
function renderOpponents(rivals) {
  if (!rivals.length) return;
  const section = document.createElement("section");
  section.className = "moment-group opponent";
  const heading = document.createElement("h3");
  heading.textContent = "對手觀察";
  const hint = document.createElement("p");
  hint.className = "muted";
  hint.textContent = "電腦用同樣的方法檢查了對手的每一步：他有什麼習慣、在哪幾步出現過，以及下次可以怎麼應對。";
  const list = document.createElement("ol");
  list.className = "entries";
  section.append(heading, hint, list);
  for (const r of rivals) {
    const li = entry(`對手 ${r.name || seatName(r.seat)}（${seatName(r.seat)}）`, r.facts);
    list.append(li);
    queueNarration(li, "narrate/", { facts: r.facts }, r.facts, "AI 依電腦的檢查整理");
    if (r.signs.length) {
      const signs = document.createElement("ol");
      signs.className = "entries moments opponent-signs";
      for (const sign of r.signs) {
        const item = document.createElement("li");
        item.textContent = `第 ${sign.index + 1} ${STEP} ${actionText(sign.played)}：${plainFacts(sign.title)}`;
        item.addEventListener("click", (event) => {
          event.stopPropagation();
          showMoment(sign, item);
        });
        signs.append(item);
      }
      li.append(signs);
    }
  }
  // 整體評論後面（沒有整體評論時放最前面）
  const overall = $("moments").querySelector(".moment-group.overall");
  if (overall) overall.after(section);
  else $("moments").prepend(section);
}

// 窄螢幕（和 style.css 的 880px 斷點相同）AI 教練排在棋盤下方：點了一則說明，把棋盤捲回畫面
const NARROW = window.matchMedia("(max-width: 880px)");
function revealBoard() {
  if (NARROW.matches) canvas.scrollIntoView({ behavior: "smooth", block: "center" });
}

function showMoment(m, li) {
  revealBoard();
  goTo(m.index);
  li.classList.add("active");
  state.marks = { played: m.played, best: m.best, seat: state.frames[m.index].to_move };
  updateNav();
  draw();
}

function goTo(n) {
  state.viewAt = Math.max(0, Math.min(state.actions.length, n));
  state.marks = null;
  state.highlight = null;
  document.querySelectorAll(".entries > li.active").forEach((el) => el.classList.remove("active"));
  updateNav();
  draw();
}

function updateNav() {
  $("nav-label").textContent = `第 ${state.viewAt} / ${state.actions.length} ${STEP}`;
  const toMove = state.frames[state.viewAt]?.to_move;
  // 輸入框上方標出正在問哪個局面（說明文字在問 AI 分頁中間的問候語裡）
  $("ask-context").textContent = `問第 ${state.viewAt} ${STEP}之後的局面${toMove ? `・輪到${seatName(toMove)}` : ""}`;
  $("mark-legend").hidden = !state.marks;
  if (state.marks) {
    $("legend-played").textContent = actionText(state.marks.played);
    $("legend-best").textContent = actionText(state.marks.best);
  }
}

// ---- 問 AI ----

let asking = false;

// 一問一答（2026-11-18 參考 Gemini 側欄改成對話的樣子）：玩家的問題是靠右的對話泡泡，
// AI 的回答接在下面（證據標籤、重點、說明、電腦分析原文照舊）；點回答回到問的那個局面
async function askQuestion(question) {
  if (!question || state.gameId === null || asking || !state.reviewing) return;
  const { round, viewAt: at } = state;
  const turn = document.createElement("li");
  turn.className = "qa";
  const where = document.createElement("span");
  where.className = "qa-where";
  where.textContent = `第 ${at} ${STEP}之後`;
  const bubble = document.createElement("p");
  bubble.className = "qa-question";
  bubble.textContent = question;
  const pending = document.createElement("div");
  pending.className = "qa-pending";
  pending.innerHTML = '<span class="dots" aria-hidden="true"><i></i><i></i><i></i></span>電腦分析中…';
  turn.append(where, bubble, pending);
  $("answers").append(turn);
  $("ask-welcome").hidden = true;
  turn.scrollIntoView({ block: "nearest" });
  asking = true;
  updateAskAvailability();
  try {
    const data = await api(`games/${state.gameId}/ask/`, { question, at, model: $("model").value });
    if (round !== state.round) return;
    const answer = document.createElement("div");
    answer.className = "qa-answer";
    answer.append(...entry("", data.facts).childNodes); // 和其他說明同一套：證據標籤、重點、說明、電腦分析原文
    showResult(answer, data, "AI 依電腦分析回答・點這裡回到那個局面");
    answer.addEventListener("click", () => {
      revealBoard();
      goTo(at);
    });
    pending.replaceWith(answer);
    turn.scrollIntoView({ block: "nearest" });
  } catch (err) {
    pending.className = "qa-pending failed";
    pending.textContent = `沒有回答：${err.message}`;
  } finally {
    asking = false;
    updateAskAvailability();
  }
}

function ask(event) {
  event.preventDefault();
  const input = $("ask-input");
  const question = input.value.trim();
  if (!question || asking) return;
  input.value = "";
  askQuestion(question);
}

// 參考表打開時，標出玩家目前的起手牌那一列並捲到那裡（GameBoard.referenceKey 由各遊戲提供）
function markReferenceRow() {
  const key = GameBoard.referenceKey?.(currentFrame());
  let mine = null;
  for (const row of document.querySelectorAll("#reference-panel tbody tr")) {
    row.classList.toggle("mine", row.dataset.key === key);
    if (row.dataset.key === key) mine = row;
  }
  mine?.scrollIntoView({ block: "center" });
}

// ---- 打開存檔的對局 ----

// 從首頁或對局紀錄頁進來（網址 ?review=編號 或 ?resume=編號）。
// resume：沒下完而且輪到玩家時接著下；其他情況（已結束、或要復盤）都直接復盤
async function openGame(id, mode) {
  try {
    const g = await api(`games/${id}/`);
    // 房間網址停在打完的那一盤（打完到回到房間之間重新整理、上一頁）：回到房間現在的那一盤，
    // 不把打完的那盤當成復盤打開，否則 AI 教練出現在房間裡（2026-11-16 使用者回報）。復盤要從「對戰紀錄」打開
    const now = mode !== "review" ? g.game.current_round : null;
    if (now) {
      window.location.replace(`/${now.game_type}/?${mode === "watch" ? "watch" : "resume"}=${now.id}`);
      return;
    }
    // 觀戰（2026-11-08）：伺服器說這盤沒有自己的座位（my_seat 是 null）
    const watching = g.game.mode === "pvp" && g.my_seat === null;
    resetView(watching ? null : g.my_seat ?? g.game.human_seat, g.game.settings, g.actions, g.frames,
      g.game.mode === "pvp", g.game.players || {}, watching);
    setRoomPeople(g.game);
    state.gameId = g.game.id;
    // 房主（可以轉讓，所以照伺服器說的）：回到這頁也能接著改設定
    state.owner = Boolean(g.is_owner);
    state.roomPassword = g.room_password ?? ""; // 房間鎖的密碼，只有房主拿得到（2026-11-18）
    state.myToken = g.my_token ?? null; // 撲克開放牌桌：自己的成員代號（看自己在不在隊列裡）
    // 從對戰紀錄復盤某一盤時網址保持 ?review=（換成 ?resume= 的話，重新整理會被帶回房間）
    if (mode !== "review") rememberRoom();
    setStartsIn(g.game.starts_in);
    renderChat(g.chat || []);
    for (const note of g.notes) addNote(note, false);
    if (!g.notes.length && g.actions.length) {
      $("thoughts-empty").textContent = "這盤棋沒有保存 AI 思路（可能是加入這個功能之前下的）。";
    }
    if (state.closed) { // 房主在開始前關掉的房間：沒有東西可以下、可以復盤
      roomClosed();
      return;
    }
    const finished = Boolean(g.game.winner || g.game.draw || g.game.ended);
    // 真人對戰沒下完時一律接著下（可能在等對手），並連上即時同步；不能先復盤
    if (watching && finished) {
      finishGame(g.game.winner, g.game.draw);
      watchReplay();
      return;
    }
    if (state.pvp && !finished) {
      syncSettingsForm(g.my_seat, g.game.settings); // 開始後重新整理也顯示房間的設定，不是「對手：AI」（2026-11-20）
      showInvite(g.game.room_code);
      openSocket(g.game.id);
      updateButtons();
      statusAfterMove(g.game);
      return;
    }
    if (mode === "resume" && !finished && currentFrame().to_move === state.human) {
      updateButtons();
      yourTurnStatus();
      return;
    }
    state.over = true;
    await startReview();
  } catch (err) {
    setStatus(`無法打開對局：${err.message}`);
  }
}

// ---- AI 模型 ----

let modelInfo = { online: false, models: [] };
const usableModel = (m) => m.installed && m.agent;

function modelOptionText(m) {
  if (!m.installed) return `${m.label}（伺服器上沒有）`;
  if (!m.agent) return `${m.label}（太慢，不能下棋）`;
  const b = m.benchmark;
  if (!b || b.agent_success_rate === undefined) return `${m.label}（還沒測過速度）`;
  const failures = b.agent_success_rate < 1 ? `，成功率 ${Math.round(b.agent_success_rate * 100)}%` : "";
  return `${m.label}　每步約 ${b.seconds} 秒${failures}`;
}

// 一切正常時不顯示；連不上或選的模型不能用時，用白話說明對玩家有什麼影響
function updateModelStatus() {
  const el = $("llm-status");
  const m = modelInfo.models.find((x) => x.name === $("model").value);
  // 連不上時，上方的 AI 伺服器說明已經講了原因，這裡不再重複
  el.hidden = Boolean(modelInfo.online ? m && usableModel(m) : !$("ai-problem").hidden);
  el.textContent = modelInfo.online ? "選的 AI 模型不能用：電腦照常下棋，說明會比較簡短"
    : "AI 說明暫時連不上：電腦照常下棋，說明會比較簡短";
  el.title = modelInfo.online ? "可以在「設定 › 進階設定」換一個模型" : "連不上 AI 模型的伺服器，恢復後會自動改回來";
}

async function loadModels() {
  const select = $("model");
  try {
    modelInfo = await api("models/");
  } catch (err) {
    modelInfo = { online: false, models: [] };
  }
  select.replaceChildren(...modelInfo.models.map((m) => {
    const option = document.createElement("option");
    option.value = m.name;
    option.textContent = modelOptionText(m);
    option.disabled = !usableModel(m);
    return option;
  }));
  const usable = (name) => modelInfo.models.some((m) => m.name === name && usableModel(m));
  const wanted = [storageGet(MODEL_KEY), modelInfo.default].find(usable);
  if (wanted) select.value = wanted;
  updateModelStatus();
}

// ---- 事件 ----

$("model").addEventListener("change", () => {
  storageSet(MODEL_KEY, $("model").value);
  updateModelStatus();
});

const savedSteps = Number(storageGet(STEPS_KEY));
if (savedSteps >= CONFIG.minSteps && savedSteps <= CONFIG.maxSteps) $("steps").value = savedSteps;
$("steps").addEventListener("change", () => {
  const n = Math.min(CONFIG.maxSteps, Math.max(CONFIG.minSteps, Math.round(Number($("steps").value)) || CONFIG.defaultSteps));
  $("steps").value = n;
  storageSet(STEPS_KEY, String(n));
});

// 還沒下棋時改「我執」或開局設定立即生效；已經開始下了就等下一局。
// 真人對戰要按「新對局」才開房間（不然每改一次設定就多開一個房間）
document.querySelectorAll("#opponent, #human-seat, .game-setting").forEach((el) => {
  el.addEventListener("change", () => {
    updateOpponentFields();
    if (state.busy) return;
    if (canChangeRoom()) changeRoom();
    else if (pvpChosen()) setStatus("真人對戰：設定好之後按「新對局」開房間，再把房間編號傳給朋友。");
    else if (state.actions.length === 0 && !state.pvp) newGame();
    else setStatus("新的設定會在按「新對局」後生效。");
  });
});
$("copy-invite").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText($("invite-link").value);
    $("copy-invite").textContent = "已複製";
  } catch (err) {
    $("invite-link").select(); // 不能用剪貼簿時（例如非 https）選起來讓玩家自己複製
  }
});

for (const { action, label, confirm } of GameBoard.extraActions || []) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.addEventListener("click", () => {
    if (state.busy || !humanTurn() || (confirm && !window.confirm(confirm))) return;
    playAction(action);
  });
  $("game-actions").append(button);
}

// 「結束（不計勝負）」和認輸排在同一排；撲克的按鈕每一步都由 renderControls 重畫，所以留在下一排
if (!GameBoard.renderControls) $("game-actions").append($("end-game"));

$("coach-toggle").addEventListener("click", () => {
  setCoachOpen($("coach").hidden);
  if (!$("coach").hidden && NARROW.matches) $("coach").scrollIntoView({ behavior: "smooth", block: "start" });
});
$("coach-close").addEventListener("click", () => setCoachOpen(false));
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => selectTab(b.dataset.tab)));

for (const id of POPOVERS) {
  document.querySelector(`[aria-controls="${id}"]`).addEventListener("click", (event) => {
    event.stopPropagation();
    const open = $(id).hidden;
    closePopovers(id);
    setPopover(id, open);
    if (open && id === "reference-panel") markReferenceRow();
  });
  $(id).addEventListener("click", (event) => event.stopPropagation());
}
document.addEventListener("click", () => closePopovers());

$("start-room").addEventListener("click", startRoom);
$("close-room").addEventListener("click", closeRoom);
$("leave-room").addEventListener("click", leaveRoom);
$("room-lock-form").addEventListener("submit", (event) => {
  event.preventDefault();
  setRoomLock(true);
});
$("room-unlock").addEventListener("click", () => setRoomLock(false));
$("room-game").replaceChildren(...CONFIG.games.map((g) => new Option(g.name, g.key, false, g.key === CONFIG.key)));
$("room-game").addEventListener("change", switchGame);
$("chat-toggle").addEventListener("click", () => setChatOpen(true));
$("chat-close").addEventListener("click", () => setChatOpen(false));
$("chat-form").addEventListener("submit", sendChat);
$("new-game").addEventListener("click", newGame);
$("end-game").addEventListener("click", endGame);
$("start-review").addEventListener("click", () => {
  // 房間裡是「對戰紀錄」：開新分頁，房間頁留著（朋友按開始時還在房間裡）
  if (inRoom()) window.open(`${$("start-review").dataset.roomHistory}?room=${state.gameId}`, "_blank", "noopener");
  else startReview();
});
$("nav-first").addEventListener("click", () => goTo(0));
$("nav-prev").addEventListener("click", () => goTo(state.viewAt - 1));
$("nav-next").addEventListener("click", () => goTo(state.viewAt + 1));
$("nav-last").addEventListener("click", () => goTo(state.actions.length));
$("ask-form").addEventListener("submit", ask);
document.querySelectorAll(".quick-questions button").forEach((b) => {
  b.addEventListener("click", () => askQuestion(b.textContent));
});
$("show-numbers").addEventListener("change", (event) => {
  state.showNumbers = event.target.checked;
  draw();
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closePopovers();
    return;
  }
  // 復盤時用方向鍵逐手翻；在輸入框打字時不攔截
  if (!state.reviewing || event.target.closest("input, select, textarea")) return;
  const jumps = {
    ArrowLeft: state.viewAt - 1, ArrowRight: state.viewAt + 1, Home: 0, End: state.actions.length,
  };
  if (event.key in jumps) {
    event.preventDefault();
    goTo(jumps[event.key]);
  }
});

setCoachOpen(storageGet(COACH_KEY) === "1");
loadModels();
const opening = new URLSearchParams(window.location.search);
const openId = Number(opening.get("resume") || opening.get("review") || opening.get("watch"));
// 打開後把網址還原：重新整理時開新局，不會又打開同一盤（沒下完的隨時可以從首頁接著下）。
// 真人對戰的房間例外，打開後由 rememberRoom 把房間編號放回網址
window.history.replaceState(null, "", window.location.pathname);
if (openId) {
  openGame(openId, opening.has("resume") ? "resume" : opening.has("watch") ? "watch" : "review").then(() => {
    // 按「加入」時座位已經坐滿，伺服器改成觀戰（2026-11-13）
    // 狀態列會被即時同步的第一次推送蓋掉，所以放在頂端的提示（10 秒後收起）
    if (opening.has("full") && state.watching) showNotice("座位已經坐滿，改成觀戰。開始前可以在聊天室和大家聊天。");
  });
}
else if (opening.get("room") === "new") createRoom();
else newGame();
