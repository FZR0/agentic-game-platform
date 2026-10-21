"use strict";

// 象棋棋盤元件：只負責畫圖與座標換算，對局流程在 arena/app.js。
// frame 由伺服器提供：board 是 90 個字元（由第 0 列往上、每列由 a 起；. 空，紅方大寫、黑方小寫，FEN 字母）；
// last 是最後一步（ICCS，例如 h2e2）；legal 是輪到的一方所有合法步；check 是輪到的一方是否被將軍；result 是終局說明。
// 走一步要點兩下（先選棋子、再點目的地），所以多提供 select；能走到哪裡完全照伺服器給的 legal，不在瀏覽器算規則。
window.GameBoard = (() => {
  const FILES = "abcdefghi";
  const NAMES = { K: "帥", A: "仕", B: "相", N: "傌", R: "俥", C: "炮", P: "兵",
                  k: "將", a: "士", b: "象", n: "馬", r: "車", c: "砲", p: "卒" };
  const ACTION_TEXT = { resign: "認輸" };
  // 開局局面與紅方第一步的 44 個合法步：對局建立前（玩家執紅先走）就要能點。和 xiangqi/rules.py 相同，測試會比對
  const START = "RNBAKABNR..........C.....C.P.P.P.P.P..................p.p.p.p.p.c.....c..........rnbakabnr";
  const START_LEGAL = ("a0a1 a0a2 a3a4 b0a2 b0c2 b2a2 b2b1 b2b3 b2b4 b2b5 b2b6 b2b9 b2c2 b2d2 b2e2 b2f2 b2g2 c0a2 "
    + "c0e2 c3c4 d0e1 e0e1 e3e4 f0e1 g0e2 g0i2 g3g4 h0g2 h0i2 h2c2 h2d2 h2e2 h2f2 h2g2 h2h1 h2h3 h2h4 h2h5 h2h6 "
    + "h2h9 h2i2 i0i1 i0i2 i3i4").split(" ");

  const PAD_Y = 46; // 下緣要留位置給欄的字母，不然會被第 0 列的棋子蓋住
  let CELL = 0;
  let PAD_X = 0;
  let selected = null; // 玩家選好的棋子（例如 "h2"）
  let selectedFrame = null; // 選棋子時的局面；局面換了就取消選擇
  // 玩家點了自己的棋子、但伺服器的 legal 裡它沒有任何一步：畫灰色虛線圈並在狀態列說明。
  // 不說明的話點了沒反應，看起來像壞掉（2026-10-16 使用者回報，實際是傌被自己的炮、俥和蹩馬腿擋住）
  let stuck = null;

  const toPixel = (f, r) => [PAD_X + f * CELL, PAD_Y + (9 - r) * CELL];
  // "e2" → [4, 2]；格式不對時回傳 null
  function parse(name) {
    const f = FILES.indexOf(name?.[0]);
    const r = Number(name?.[1]);
    return name?.length === 2 && f >= 0 && Number.isInteger(r) ? [f, r] : null;
  }
  const at = (frame, name) => { const p = parse(name); return p ? frame.board[p[1] * 9 + p[0]] : "."; };
  const isRed = (piece) => piece !== "." && piece === piece.toUpperCase();
  const targets = (frame, from) => (frame.legal || []).filter((m) => m.startsWith(from)).map((m) => m.slice(2));

  function currentSelection(frame) {
    if (selectedFrame !== frame) {
      selected = null;
      stuck = null;
    }
    return selected;
  }

  function piece(ctx, name, p, alpha = 1) {
    const pos = parse(name);
    if (!pos || p === ".") return;
    const [px, py] = toPixel(...pos);
    const color = isRed(p) ? "#c0392b" : "#1d1d1d";
    ctx.globalAlpha = alpha;
    ctx.beginPath();
    ctx.arc(px, py, CELL * 0.44, 0, Math.PI * 2);
    ctx.fillStyle = "#f6e3bf";
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = color;
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(px, py, CELL * 0.37, 0, Math.PI * 2);
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.fillStyle = color;
    ctx.font = `bold ${Math.round(CELL * 0.46)}px "Microsoft JhengHei", "Noto Sans TC", sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(NAMES[p], px, py + 1);
    ctx.globalAlpha = 1;
  }

  function ring(ctx, name, color, dash = [], width = 3) {
    const pos = parse(name);
    if (!pos) return;
    const [px, py] = toPixel(...pos);
    ctx.save();
    ctx.setLineDash(dash);
    ctx.lineWidth = width;
    ctx.strokeStyle = color;
    ctx.beginPath();
    ctx.arc(px, py, CELL * 0.49, 0, Math.PI * 2);
    ctx.stroke();
    ctx.restore();
  }

  function dot(ctx, name, color) {
    const pos = parse(name);
    if (!pos) return;
    const [px, py] = toPixel(...pos);
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(px, py, CELL * 0.12, 0, Math.PI * 2);
    ctx.fill();
  }

  // 一步（h2e2）畫成箭頭：起點到終點
  function arrow(ctx, move, color, dash = []) {
    const a = parse(move?.slice(0, 2));
    const b = parse(move?.slice(2, 4));
    if (!a || !b) return;
    const [x1, y1] = toPixel(...a);
    const [x2, y2] = toPixel(...b);
    const angle = Math.atan2(y2 - y1, x2 - x1);
    ctx.save();
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = 5;
    ctx.setLineDash(dash);
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2 - Math.cos(angle) * 14, y2 - Math.sin(angle) * 14);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.beginPath();
    ctx.moveTo(x2, y2);
    ctx.lineTo(x2 - Math.cos(angle - 0.45) * 22, y2 - Math.sin(angle - 0.45) * 22);
    ctx.lineTo(x2 - Math.cos(angle + 0.45) * 22, y2 - Math.sin(angle + 0.45) * 22);
    ctx.closePath();
    ctx.fill();
    ctx.restore();
  }

  function grid(ctx, canvas) {
    ctx.fillStyle = "#e3c48f";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.strokeStyle = "#5a3e1b";
    ctx.lineWidth = 1.2;
    const line = (f1, r1, f2, r2) => {
      const [x1, y1] = toPixel(f1, r1);
      const [x2, y2] = toPixel(f2, r2);
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.stroke();
    };
    for (let r = 0; r <= 9; r++) line(0, r, 8, r);
    for (let f = 0; f <= 8; f++) {
      if (f === 0 || f === 8) {
        line(f, 0, f, 9);
      } else {
        line(f, 0, f, 4); // 河界中間不畫直線
        line(f, 5, f, 9);
      }
    }
    line(3, 0, 5, 2); // 九宮的斜線
    line(5, 0, 3, 2);
    line(3, 7, 5, 9);
    line(5, 7, 3, 9);
    ctx.fillStyle = "#5a3e1b";
    ctx.font = `${Math.round(CELL * 0.42)}px "Microsoft JhengHei", "Noto Sans TC", sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    const [, riverY] = toPixel(0, 4.5);
    ctx.fillText("楚　河", PAD_X + CELL * 2, riverY);
    ctx.fillText("漢　界", PAD_X + CELL * 6, riverY);
    ctx.font = "13px system-ui, sans-serif";
    for (let f = 0; f <= 8; f++) ctx.fillText(FILES[f], toPixel(f, 0)[0], canvas.height - 9);
    for (let r = 0; r <= 9; r++) ctx.fillText(String(r), PAD_X / 2, toPixel(0, r)[1]);
  }

  function draw(canvas, view) {
    const { frame } = view;
    const ctx = canvas.getContext("2d");
    CELL = (canvas.height - PAD_Y * 2) / 9;
    PAD_X = (canvas.width - CELL * 8) / 2;
    grid(ctx, canvas);

    // 玩家剛走、伺服器還沒回應的一步先畫出來
    const board = frame.board.split("");
    if (view.pending?.length === 4) {
      const [a, b] = [parse(view.pending.slice(0, 2)), parse(view.pending.slice(2))];
      if (a && b) {
        board[b[1] * 9 + b[0]] = board[a[1] * 9 + a[0]];
        board[a[1] * 9 + a[0]] = ".";
      }
    }
    const last = view.pending || frame.last;
    if (last?.length === 4) {
      ring(ctx, last.slice(0, 2), "rgba(232, 69, 60, 0.6)", [4, 3], 2);
      ring(ctx, last.slice(2), "#e8453c", [], 3);
    }
    board.forEach((p, i) => piece(ctx, FILES[i % 9] + Math.floor(i / 9), p));
    if (frame.check && !frame.result) {
      const king = board.indexOf(frame.to_move === 1 ? "K" : "k");
      if (king >= 0) ring(ctx, FILES[king % 9] + Math.floor(king / 9), "#d93025", [], 5);
    }

    const sel = view.pending ? null : currentSelection(frame);
    if (sel) {
      ring(ctx, sel, "#1e8e3e", [], 4);
      for (const t of targets(frame, sel)) dot(ctx, t, "rgba(30, 142, 62, 0.75)");
    }
    if (stuck && !view.pending) ring(ctx, stuck, "rgba(90, 90, 90, 0.85)", [5, 4], 3);
    if (view.hover && view.hover !== sel) ring(ctx, view.hover, "rgba(30, 142, 62, 0.6)", [], 2);
    if (view.marks) {
      arrow(ctx, view.marks.played, "rgba(217, 48, 37, 0.85)");
      arrow(ctx, view.marks.best, "rgba(30, 142, 62, 0.85)", [8, 6]);
    }
    if (view.spot) {
      // 說明中的座標：一格（e2）畫一圈；一步（h2e2）畫箭頭
      if (view.spot.length === 4) arrow(ctx, view.spot, "rgba(26, 115, 232, 0.8)");
      else ring(ctx, view.spot, "#1a73e8");
    }
  }

  // 滑鼠位置對應的格子名稱（例如 "e2"），棋盤外回傳 null
  function pointAt(canvas, event) {
    if (!CELL) return null;
    const rect = canvas.getBoundingClientRect();
    const px = (event.clientX - rect.left) * (canvas.width / rect.width);
    const py = (event.clientY - rect.top) * (canvas.height / rect.height);
    const f = Math.round((px - PAD_X) / CELL);
    const r = 9 - Math.round((py - PAD_Y) / CELL);
    return f >= 0 && f <= 8 && r >= 0 && r <= 9 ? FILES[f] + r : null;
  }

  // 能點的格子：有合法步的自己棋子，或選好的棋子能走到的位置
  function canPlay(frame, name) {
    const sel = currentSelection(frame);
    return (sel !== null && targets(frame, sel).includes(name)) || targets(frame, name).length > 0;
  }

  // 點一下：選棋子或走到目的地。走到目的地時回傳那一步（例如 "h2e2"），否則回傳 null
  function select(frame, name) {
    const sel = currentSelection(frame);
    stuck = null;
    if (sel !== null && targets(frame, sel).includes(name)) {
      selected = null;
      return sel + name;
    }
    // 是不是自己的棋子只看大小寫（畫圖本來就用它分顏色）；能不能走完全照伺服器的 legal
    const own = at(frame, name) !== "." && isRed(at(frame, name)) === (frame.to_move === 1);
    if (own && targets(frame, name).length === 0) stuck = name;
    selected = name && targets(frame, name).length > 0 && name !== sel ? name : null;
    selectedFrame = frame;
    return null;
  }

  const emptyFrame = () => ({ board: START, last: null, to_move: 1, check: false, legal: START_LEGAL, result: null });

  const turnHint = (frame) => [
    frame.check && !frame.result ? "你被將軍了，這一步一定要應將。" : "",
    stuck && selectedFrame === frame
      ? "這顆棋子現在沒有可以走的位置：可能被擋住（包括蹩馬腿、塞象眼），或是走了會讓自己被將軍。請換一顆。" : "",
  ].join("");

  return {
    draw, pointAt, canPlay, select, emptyFrame, turnHint,
    actionText: (action) => ACTION_TEXT[action] ?? action,
    extraActions: [{ action: "resign", label: "認輸", confirm: "確定要認輸嗎？" }],
  };
})();
