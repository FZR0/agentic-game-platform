"use strict";

// 五子棋棋盤元件：只負責畫圖與座標換算，對局流程在 arena/app.js。
// frame 由伺服器提供：board 是 225 個字元（由第 1 列往上、每列 A 到 O；. 空、B 黑、W 白），last 是最後一手。
window.GameBoard = (() => {
  const SIZE = 15;
  const COLS = "ABCDEFGHIJKLMNO";
  const PAD = 36;
  const STARS = [[3, 3], [11, 3], [7, 7], [3, 11], [11, 11]];
  const STONE = { B: "#111111", W: "#f8f8f8" };
  const SEAT_STONE = { 1: "B", 2: "W" };

  let CELL = 0;
  const toPixel = (x, y, canvas) => [PAD + x * CELL, PAD + (SIZE - 1 - y) * CELL];
  const parse = (name) => [COLS.indexOf(name[0]), Number(name.slice(1)) - 1];
  const nameOf = (x, y) => COLS[x] + (y + 1);

  function stone(ctx, canvas, name, color, alpha = 1, scale = 1) {
    const [px, py] = toPixel(...parse(name), canvas);
    ctx.globalAlpha = alpha;
    ctx.beginPath();
    ctx.arc(px, py, CELL * 0.44 * scale, 0, Math.PI * 2);
    ctx.fillStyle = STONE[color];
    ctx.fill();
    ctx.strokeStyle = "#333333";
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.globalAlpha = 1;
  }

  function ring(ctx, canvas, name, color, dash) {
    const [px, py] = toPixel(...parse(name), canvas);
    ctx.save();
    ctx.setLineDash(dash);
    ctx.lineWidth = 3;
    ctx.strokeStyle = color;
    ctx.beginPath();
    ctx.arc(px, py, CELL * 0.5, 0, Math.PI * 2);
    ctx.stroke();
    ctx.restore();
  }

  // 說明中的座標：用十字線拉出所在的行列，再畫一圈，找點比較容易
  function spot(ctx, canvas, name) {
    const [px, py] = toPixel(...parse(name), canvas);
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
    ring(ctx, canvas, name, "#1a73e8", []);
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
    const ctx = canvas.getContext("2d");
    CELL = (canvas.width - PAD * 2) / (SIZE - 1);
    ctx.fillStyle = "#e3b76a";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.strokeStyle = "#5a4320";
    ctx.fillStyle = "#5a4320";
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
    for (const [x, y] of STARS) {
      const [px, py] = toPixel(x, y, canvas);
      ctx.beginPath();
      ctx.arc(px, py, 4, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.font = "14px system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    for (let i = 0; i < SIZE; i++) {
      const [px] = toPixel(i, 0, canvas);
      const [, py] = toPixel(0, i, canvas);
      ctx.fillText(COLS[i], px, canvas.height - PAD / 2 + 4);
      ctx.fillText(String(i + 1), PAD / 2 - 4, py);
    }

    const { frame } = view;
    for (let i = 0; i < frame.board.length; i++) {
      const c = frame.board[i];
      const name = nameOf(i % SIZE, Math.floor(i / SIZE));
      if (c !== ".") stone(ctx, canvas, name, c, 1, name === popped ? grow : 1);
    }
    if (view.showNumbers) {
      ctx.font = "bold 15px system-ui, sans-serif";
      view.actions.forEach((name, i) => {
        if (name === "resign") return; // 認輸不是棋子，沒有位置
        const [px, py] = toPixel(...parse(name), canvas);
        const last = i === view.actions.length - 1;
        ctx.fillStyle = last ? "#e8453c" : i % 2 === 0 ? "#f8f8f8" : "#111111";
        ctx.fillText(String(i + 1), px, py + 1);
      });
    } else if (frame.last) {
      const [px, py] = toPixel(...parse(frame.last), canvas);
      ctx.fillStyle = "#e8453c";
      ctx.beginPath();
      ctx.arc(px, py, 5, 0, Math.PI * 2);
      ctx.fill();
    }
    if (view.pending) stone(ctx, canvas, view.pending, SEAT_STONE[view.hoverSeat], 1, view.pending === popped ? grow : 1);
    if (view.hover) stone(ctx, canvas, view.hover, SEAT_STONE[view.hoverSeat], 0.4);
    if (view.marks) {
      stone(ctx, canvas, view.marks.played, SEAT_STONE[view.marks.seat], 0.45);
      ring(ctx, canvas, view.marks.played, "#d93025", []);
      ring(ctx, canvas, view.marks.best, "#1e8e3e", [6, 4]);
    }
    if (view.spot) spot(ctx, canvas, view.spot);
  }

  // 滑鼠位置對應的交叉點名稱，棋盤外回傳 null
  function pointAt(canvas, event) {
    const rect = canvas.getBoundingClientRect();
    const px = (event.clientX - rect.left) * (canvas.width / rect.width);
    const py = (event.clientY - rect.top) * (canvas.height / rect.height);
    const x = Math.round((px - PAD) / CELL);
    const y = SIZE - 1 - Math.round((py - PAD) / CELL);
    return x >= 0 && x < SIZE && y >= 0 && y < SIZE ? nameOf(x, y) : null;
  }

  function canPlay(frame, name) {
    const [x, y] = parse(name);
    return frame.board[y * SIZE + x] === ".";
  }

  // 開局（還沒建立存檔時用來畫空棋盤）：黑棋先下
  const emptyFrame = () => ({ board: ".".repeat(SIZE * SIZE), last: null, to_move: 1 });

  return {
    draw, pointAt, canPlay, emptyFrame,
    actionText: (action) => (action === "resign" ? "認輸" : action),
    extraActions: [{ action: "resign", label: "認輸", confirm: "確定要認輸嗎？這盤會算對方贏。" }],
  };
})();
