"use strict";

// 圍棋棋盤元件：只負責畫圖與座標換算，對局流程在 arena/app.js。
// frame 由伺服器提供：size 路數；board 是 size×size 個字元（由第 1 列往上、每列由 A 起；. 空、B 黑、W 白）；
// last 是最後一個行動（座標、pass 或 resign）；result 是終局說明。座標是 GTP 慣例，欄跳過 I。
window.GameBoard = (() => {
  const COLS = "ABCDEFGHJKLMNOPQRST";
  const PAD = 36;
  const STARS = { 19: [3, 9, 15], 13: [3, 6, 9], 9: [2, 4, 6] };
  const STONE = { B: "#111111", W: "#f8f8f8" };
  const SEAT_STONE = { 1: "B", 2: "W" };
  const ACTION_TEXT = { pass: "虛手", resign: "認輸" };

  let SIZE = 19;
  let CELL = 0;
  const toPixel = (x, y) => [PAD + x * CELL, PAD + (SIZE - 1 - y) * CELL];
  // 座標 → [x, y]；pass、resign 或超出棋盤時回傳 null
  function parse(name) {
    const x = COLS.indexOf(name?.[0]);
    const y = Number(name?.slice(1)) - 1;
    return x >= 0 && x < SIZE && Number.isInteger(y) && y >= 0 && y < SIZE ? [x, y] : null;
  }
  const nameOf = (x, y) => COLS[x] + (y + 1);

  function stone(ctx, name, color, alpha = 1, scale = 1) {
    const p = parse(name);
    if (!p) return;
    const [px, py] = toPixel(...p);
    ctx.globalAlpha = alpha;
    ctx.beginPath();
    ctx.arc(px, py, CELL * 0.46 * scale, 0, Math.PI * 2);
    ctx.fillStyle = STONE[color];
    ctx.fill();
    ctx.strokeStyle = "#333333";
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.globalAlpha = 1;
  }

  function ring(ctx, name, color, dash) {
    const p = parse(name);
    if (!p) return;
    const [px, py] = toPixel(...p);
    ctx.save();
    ctx.setLineDash(dash);
    ctx.lineWidth = 3;
    ctx.strokeStyle = color;
    ctx.beginPath();
    ctx.arc(px, py, CELL * 0.52, 0, Math.PI * 2);
    ctx.stroke();
    ctx.restore();
  }

  // 說明中的座標：用十字線拉出所在的行列，再畫一圈，找點比較容易
  function spot(ctx, canvas, name) {
    const p = parse(name);
    if (!p) return;
    const [px, py] = toPixel(...p);
    ctx.save();
    ctx.strokeStyle = "rgba(26, 115, 232, 0.55)";
    ctx.lineWidth = 3;
    ctx.beginPath();
    ctx.moveTo(PAD, py);
    ctx.lineTo(canvas.width - PAD, py);
    ctx.moveTo(px, PAD);
    ctx.lineTo(px, canvas.height - PAD);
    ctx.stroke();
    ctx.restore();
    ring(ctx, name, "#1a73e8", []);
  }

  // 新落下的一子放大進場（動畫）；系統要求減少動態時不播。局面一律是伺服器給的 frame
  // 「新的一子」是玩家剛點、還在等伺服器的那一子，或局面上的最後一手；同一子只播一次，伺服器確認時不再重播
  const DROP_MS = 180;
  const REDUCED = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  let popped = null;
  let first = true; // 剛打開頁面時的棋子不播
  let since = -Infinity;
  let pending = 0;
  let lastCall = null;

  function draw(canvas, view) {
    const newest = view.pending || view.frame.last;
    if (newest !== popped || first) {
      since = !first && !REDUCED ? performance.now() : -Infinity;
      popped = newest;
      first = false;
    }
    lastCall = [canvas, view];
    const p = (performance.now() - since) / DROP_MS;
    render(canvas, view, p < 1 ? 0.5 + 0.5 * (1 - (1 - p) ** 3) : 1);
    if (p < 1 && !pending) {
      pending = requestAnimationFrame(() => {
        pending = 0;
        draw(...lastCall);
      });
    }
  }

  function render(canvas, view, grow) {
    const { frame } = view;
    const ctx = canvas.getContext("2d");
    SIZE = frame.size;
    CELL = (canvas.width - PAD * 2) / (SIZE - 1);
    ctx.fillStyle = "#dcb36b";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.strokeStyle = "#4a3518";
    ctx.fillStyle = "#4a3518";
    ctx.lineWidth = 1;
    for (let i = 0; i < SIZE; i++) {
      const p = PAD + i * CELL;
      ctx.beginPath();
      ctx.moveTo(PAD, p);
      ctx.lineTo(canvas.width - PAD, p);
      ctx.moveTo(p, PAD);
      ctx.lineTo(p, canvas.height - PAD);
      ctx.stroke();
    }
    const stars = STARS[SIZE] || [];
    for (const x of stars) {
      for (const y of stars) {
        // 9 路與 13 路只畫四角和天元
        if (SIZE !== 19 && (x === stars[1]) !== (y === stars[1])) continue;
        const [px, py] = toPixel(x, y);
        ctx.beginPath();
        ctx.arc(px, py, 3.5, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    ctx.font = "13px system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    for (let i = 0; i < SIZE; i++) {
      const [px] = toPixel(i, 0);
      const [, py] = toPixel(0, i);
      ctx.fillText(COLS[i], px, canvas.height - PAD / 2 + 4);
      ctx.fillText(String(i + 1), PAD / 2 - 4, py);
    }

    for (let i = 0; i < frame.board.length; i++) {
      const c = frame.board[i];
      const name = nameOf(i % SIZE, Math.floor(i / SIZE));
      if (c !== ".") stone(ctx, name, c, 1, name === popped ? grow : 1);
    }
    if (view.showNumbers) {
      // 只標還在盤上的子：同一點被提掉又重下時，標最後一次落子的手數
      const placed = new Map();
      view.actions.forEach((name, i) => { if (parse(name)) placed.set(name, i); });
      ctx.font = `bold ${SIZE === 19 ? 11 : 14}px system-ui, sans-serif`;
      for (const [name, i] of placed) {
        const [x, y] = parse(name);
        const c = frame.board[y * SIZE + x];
        if (c === ".") continue;
        const [px, py] = toPixel(x, y);
        ctx.fillStyle = i === view.actions.length - 1 ? "#e8453c" : c === "B" ? "#f8f8f8" : "#111111";
        ctx.fillText(String(i + 1), px, py + 1);
      }
    } else if (parse(frame.last)) {
      const [px, py] = toPixel(...parse(frame.last));
      ctx.fillStyle = "#e8453c";
      ctx.beginPath();
      ctx.arc(px, py, Math.max(3, CELL * 0.15), 0, Math.PI * 2);
      ctx.fill();
    }
    if (view.pending) stone(ctx, view.pending, SEAT_STONE[view.hoverSeat], 1, view.pending === popped ? grow : 1);
    if (view.hover) stone(ctx, view.hover, SEAT_STONE[view.hoverSeat], 0.4);
    if (view.marks) {
      stone(ctx, view.marks.played, SEAT_STONE[view.marks.seat], 0.45);
      ring(ctx, view.marks.played, "#d93025", []);
      ring(ctx, view.marks.best, "#1e8e3e", [6, 4]);
    }
    if (view.spot) spot(ctx, canvas, view.spot);
  }

  // 滑鼠位置對應的交叉點名稱，棋盤外回傳 null
  function pointAt(canvas, event) {
    if (!CELL) return null;
    const rect = canvas.getBoundingClientRect();
    const px = (event.clientX - rect.left) * (canvas.width / rect.width);
    const py = (event.clientY - rect.top) * (canvas.height / rect.height);
    const x = Math.round((px - PAD) / CELL);
    const y = SIZE - 1 - Math.round((py - PAD) / CELL);
    return x >= 0 && x < SIZE && y >= 0 && y < SIZE ? nameOf(x, y) : null;
  }

  // 只檢查空點；自殺與劫由伺服器判定，不合法時會回傳原因
  function canPlay(frame, name) {
    const p = parse(name);
    return Boolean(p) && frame.board[p[1] * frame.size + p[0]] === ".";
  }

  const emptyFrame = (settings) => {
    const size = Number(settings.size) || 19;
    return { size, board: ".".repeat(size * size), last: null, to_move: 1, captures: { 1: 0, 2: 0 }, passes: 0, result: null };
  };

  // 對手剛虛手時提醒終局規則：盤上的子一律算活
  const turnHint = (frame) => (frame.passes === 1
    ? "對手虛手了。你也虛手的話就照盤面數子（盤上的子一律算活，對方的死子要先提掉）。"
    : "");

  return {
    draw, pointAt, canPlay, emptyFrame, turnHint,
    actionText: (action) => ACTION_TEXT[action] ?? action,
    extraActions: [
      { action: "pass", label: "虛手" },
      { action: "resign", label: "認輸", confirm: "確定要認輸嗎？" },
    ],
  };
})();
