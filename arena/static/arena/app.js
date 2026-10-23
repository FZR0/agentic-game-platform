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
const seatName = (seat) => CONFIG.seatNames[seat] ?? `座位 ${seat}`;
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
};
// 說明一次只送一個請求給 Ollama，依序產生
let narrationQueue = Promise.resolve();

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
  let data;
  try {
    data = await res.json();
  } catch (err) {
    // 伺服器程式出錯時回傳的是錯誤網頁、不是 JSON；給玩家看得懂的訊息，細節在伺服器視窗
    throw new Error(`伺服器發生錯誤（${res.status}），這一步沒有存檔`);
  }
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
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
  const enabled = state.reviewing && state.gameId !== null && !asking;
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
  $("game-status").textContent = text;
}

// Agent 一手要十幾秒，顯示經過的秒數，讓人知道它還在查證
function setThinkingStatus() {
  const started = Date.now();
  setStatus("AI 思考中…");
  statusTimer = setInterval(() => {
    $("game-status").textContent = `AI 思考中… ${Math.round((Date.now() - started) / 1000)} 秒`;
  }, 1000);
}

const humanTurn = () => !state.over && !state.reviewing && currentFrame()?.to_move === state.human;

function updateButtons() {
  $("new-game").disabled = state.busy;
  $("start-review").disabled = state.busy || state.reviewing || state.gameId === null;
  document.querySelectorAll("#game-actions button").forEach((b) => { b.disabled = state.busy || !humanTurn(); });
  // 「結束（不計勝負）」只在有存檔、還沒結束的對局顯示；玩家先下而還沒下第一步時沒有存檔，不必結束
  $("end-game").hidden = state.gameId === null || state.over || state.reviewing;
  $("end-game").disabled = state.busy;
  GameBoard.renderControls?.(currentFrame(), $("game-actions"), !state.busy && humanTurn(), playAction);
  updateAskAvailability();
}

function agentOptions() {
  return { model: $("model").value, steps: Number($("steps").value) || CONFIG.defaultSteps };
}

function readSettings() {
  const settings = {};
  document.querySelectorAll(".game-setting").forEach((el) => {
    settings[el.dataset.name] = Number.isNaN(Number(el.value)) ? el.value : Number(el.value);
  });
  return settings;
}

function resetView(human, settings, actions, frames) {
  state.round += 1;
  Object.assign(state, {
    human, settings, actions, frames, over: false, reviewing: false, viewAt: 0, marks: null,
    hover: null, highlight: null, hoverHighlight: null, hiddenNotes: false,
  });
  setUnread(0);
  $("ai-log").replaceChildren();
  $("ai-log").classList.remove("clickable");
  $("thoughts-empty").hidden = false;
  $("thoughts-empty").textContent = "AI 每下一手，思路會出現在這裡。復盤時點一則思路，棋盤會跳到那一手。";
  $("moments").replaceChildren();
  $("review-summary").textContent = REVIEW_HINT;
  $("answers").replaceChildren();
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

function finishGame(winner, draw) {
  state.over = true;
  const result = draw ? "和局。" : winner === state.human ? "你贏了！" : `${seatName(winner)}（AI）獲勝。`;
  const detail = currentFrame()?.result ? `（${currentFrame().result}）` : "";
  setStatus(`${result}${detail}可以按上方的「復盤」檢討。`);
}

function yourTurnStatus() {
  const hint = GameBoard.turnHint?.(currentFrame()) || "";
  setStatus(`輪到你下（${seatName(state.human)}）${hint ? `。${hint}` : ""}`);
}

function statusAfterMove(data) {
  if (data.winner || data.draw) finishGame(data.winner, data.draw);
  else yourTurnStatus();
}

// 玩家先下時等第一步才建立存檔，避免重新整理頁面就多出空的對局
async function newGame() {
  closePopovers();
  const settings = readSettings();
  resetView(Number($("human-seat").value), settings, [], [GameBoard.emptyFrame(settings)]);
  state.gameId = null;
  updateButtons();
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
  setThinkingStatus();
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
const plainFacts = (text) => text.replace(TAKEOVER_RE, "AI 這一步沒有完成查證，改由電腦直接下。");

function plainNote(note) {
  if (note.startsWith("Ollama 無法使用")) return "AI 說明暫時連不上";
  if (note.startsWith("模型提到事實中沒有的座標")) return "AI 的說明提到了分析裡沒有的位置，所以不採用";
  if (note.startsWith("只有引擎估計或條件式計算")) return "AI 的說明講得比分析更肯定，所以不採用";
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
  } else if (live) {
    queueNarration(li, path, {}, note.facts, "AI 的白話說明");
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
      const li = entry(`第 ${m.index + 1} ${STEP} ${actionText(m.played)}：${m.title}`, m.facts);
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
  $("ask-context").textContent = `針對第 ${state.viewAt} ${STEP}之後的局面提問${toMove ? `（輪到${seatName(toMove)}）` : ""}。`
    + `問題中的座標、牌或「第幾${STEP}」會先交給電腦分析。`;
  $("mark-legend").hidden = !state.marks;
  if (state.marks) {
    $("legend-played").textContent = actionText(state.marks.played);
    $("legend-best").textContent = actionText(state.marks.best);
  }
}

// ---- 問 AI ----

let asking = false;

async function askQuestion(question) {
  if (!question || state.gameId === null || asking || !state.reviewing) return;
  const { round, viewAt: at } = state;
  const headText = `問（第 ${at} ${STEP}後）：${question}`;
  const pending = document.createElement("li");
  pending.textContent = `${headText}　電腦分析中…`;
  $("answers").append(pending);
  pending.scrollIntoView({ block: "nearest" });
  asking = true;
  updateAskAvailability();
  try {
    const data = await api(`games/${state.gameId}/ask/`, { question, at, model: $("model").value });
    if (round !== state.round) return;
    const li = entry(headText, data.facts);
    showResult(li, data, "AI 依電腦分析回答（點這裡回到那個局面）");
    li.addEventListener("click", () => {
      revealBoard();
      goTo(at);
    });
    pending.replaceWith(li);
    li.scrollIntoView({ block: "nearest" });
  } catch (err) {
    pending.textContent = `${headText}　失敗：${err.message}`;
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
    resetView(g.game.human_seat, g.game.settings, g.actions, g.frames);
    state.gameId = g.game.id;
    for (const note of g.notes) addNote(note, false);
    if (!g.notes.length && g.actions.length) {
      $("thoughts-empty").textContent = "這盤棋沒有保存 AI 思路（可能是加入這個功能之前下的）。";
    }
    const finished = Boolean(g.game.winner || g.game.draw || g.game.ended);
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
  el.hidden = Boolean(modelInfo.online && m && usableModel(m));
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

// 還沒下棋時改「我執」或開局設定立即生效；已經開始下了就等下一局
document.querySelectorAll("#human-seat, .game-setting").forEach((el) => {
  el.addEventListener("change", () => {
    if (state.busy) return;
    if (state.actions.length === 0) newGame();
    else setStatus("新的設定會在按「新對局」後生效。");
  });
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

$("new-game").addEventListener("click", newGame);
$("end-game").addEventListener("click", endGame);
$("start-review").addEventListener("click", startReview);
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
const openId = Number(opening.get("resume") || opening.get("review"));
// 打開後把網址還原：重新整理時開新局，不會又打開同一盤（沒下完的隨時可以從首頁接著下）
window.history.replaceState(null, "", window.location.pathname);
if (openId) openGame(openId, opening.has("resume") ? "resume" : "review");
else newGame();
