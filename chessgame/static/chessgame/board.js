"use strict";

// 國際象棋棋盤元件：只負責畫圖、座標換算，以及「先點棋子、再點目標格、升變再選棋子」的選取；對局流程在 arena/app.js。
// frame 由伺服器提供：board 是 64 個字元（第 0 個是 a1、第 1 個是 b1……第 63 個是 h8；大寫白棋、小寫黑棋、. 空格）；
// legal 是 {起點格: [終點格, ...]}，promotions 是需要選升變的「起點終點」；last 是最後一步（UCI 或 resign）；
// check 是被將軍的王所在格；result 是終局說明。哪一步合法一律由伺服器算，這裡只照 legal 標示，不判斷規則。
window.GameBoard = (() => {
  const FILES = "abcdefgh";
  const PAD = 28;
  // 一律用實心的棋子字形，白棋填白色加深色外框；︎ 要求用文字字形，不要變成彩色表情符號
  const GLYPH = { k: "♚", q: "♛", r: "♜", b: "♝", n: "♞", p: "♟" };
  const PROMO = ["q", "r", "b", "n"];
  const PROMO_NAMES = { q: "后", r: "車", b: "象", n: "馬" };
  const LIGHT = "#f0d9b5";
  const DARK = "#b58863";
  // 開局的 frame（和伺服器 ChessModule.frame(new_state) 一樣；chessgame.tests 會比對兩者，改規則或格式時要一起改）。
  // 玩家執白先下時，對局等第一步才建立，第一步只能靠這份資料標出可以走的格子
  const START = /*start-frame*/{"board":"RNBQKBNRPPPPPPPP................................pppppppprnbqkbnr","last":null,"to_move":1,"check":null,"legal":{"g1":["h3","f3"],"b1":["c3","a3"],"h2":["h3","h4"],"g2":["g3","g4"],"f2":["f3","f4"],"e2":["e3","e4"],"d2":["d3","d4"],"c2":["c3","c4"],"b2":["b3","b4"],"a2":["a3","a4"]},"promotions":[],"result":null}/*end-start-frame*/;

  let CELL = 0;
  let flipped = false;   // 玩家執黑時，黑棋放在下面
  let selected = null;   // 玩家點選的棋子所在格
  let promoting = null;  // 等玩家選升變棋子的「起點終點」
  let shownFrame = null; // 局面換了（下了一步、翻到別手）就取消選取
  // 玩家點了自己的棋子、但伺服器的 legal 裡它沒有任何一步：畫灰色虛線框並在狀態列說明，不然點了沒反應像壞掉（2026-10-16）
  let stuck = null;

  // 格子名稱 → [x, y]（x 是 a–h、y 是 1–8，都從 0 起算）；不是格子時回傳 null
  function parse(name) {
    const x = FILES.indexOf(name?.[0]);
    const y = Number(name?.[1]) - 1;
    return typeof name === "string" && name.length >= 2 && x >= 0 && y >= 0 && y < 8 ? [x, y] : null;
  }
  const nameOf = (x, y) => FILES[x] + (y + 1);
  // 格子在畫面上的欄、列（左上角是 0, 0）
  const screenOf = ([x, y]) => (flipped ? [7 - x, y] : [x, 7 - y]);
  const corner = (p) => {
    const [c, r] = screenOf(p);
    return [PAD + c * CELL, PAD + r * CELL];
  };
  const center = (p) => corner(p).map((v) => v + CELL / 2);
  const pieceAt = (board, name) => {
    const p = parse(name);
    return p ? board[p[1] * 8 + p[0]] : ".";
  };

  // 等伺服器回應時先把玩家這一步畫上去（只移動那顆子；易位的車、吃過路兵等伺服器的 frame 回來就會正確）
  function withPending(board, action) {
    const from = parse(action), to = parse(action?.slice(2));
    if (!from || !to) return board;
    const cells = board.split("");
    let piece = cells[from[1] * 8 + from[0]];
    if (action.length === 5) piece = piece === piece.toUpperCase() ? action[4].toUpperCase() : action[4];
    cells[from[1] * 8 + from[0]] = ".";
    cells[to[1] * 8 + to[0]] = piece;
    return cells.join("");
  }

  function fillSquare(ctx, p, color) {
    const [px, py] = corner(p);
    ctx.fillStyle = color;
    ctx.fillRect(px, py, CELL, CELL);
  }

  function outline(ctx, p, color, width, dash = []) {
    const [px, py] = corner(p);
    ctx.save();
    ctx.setLineDash(dash);
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.strokeRect(px + width / 2, py + width / 2, CELL - width, CELL - width);
    ctx.restore();
  }

  function piece(ctx, ch, cx, cy, alpha = 1) {
    const white = ch === ch.toUpperCase();
    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.font = `${Math.round(CELL * 0.8)}px "Segoe UI Symbol", "Noto Sans Symbols 2", "DejaVu Sans", serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    const glyph = GLYPH[ch.toLowerCase()] + "︎";
    ctx.lineJoin = "round";
    ctx.lineWidth = Math.max(2, CELL * 0.05);
    ctx.strokeStyle = white ? "#1c1c1c" : "#f5f5f5";
    ctx.strokeText(glyph, cx, cy + CELL * 0.04);
    ctx.fillStyle = white ? "#ffffff" : "#1c1c1c";
    ctx.fillText(glyph, cx, cy + CELL * 0.04);
    ctx.restore();
  }

  // 復盤時的箭頭：實際下的（紅）與引擎建議的（綠，虛線）
  function arrow(ctx, action, color, dash) {
    const from = parse(action), to = parse(action?.slice(2));
    if (!from || !to) return;
    const [x1, y1] = center(from), [x2, y2] = center(to);
    const angle = Math.atan2(y2 - y1, x2 - x1);
    const head = CELL * 0.3;
    ctx.save();
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = CELL * 0.1;
    ctx.globalAlpha = 0.8;
    ctx.setLineDash(dash);
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2 - Math.cos(angle) * head, y2 - Math.sin(angle) * head);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.beginPath();
    ctx.moveTo(x2, y2);
    ctx.lineTo(x2 - head * Math.cos(angle - 0.5), y2 - head * Math.sin(angle - 0.5));
    ctx.lineTo(x2 - head * Math.cos(angle + 0.5), y2 - head * Math.sin(angle + 0.5));
    ctx.closePath();
    ctx.fill();
    ctx.restore();
  }

  function draw(canvas, view) {
    const { frame } = view;
    if (frame !== shownFrame) {
      selected = null;
      promoting = null;
      stuck = null;
      shownFrame = frame;
    }
    flipped = view.hoverSeat === 2;
    const ctx = canvas.getContext("2d");
    CELL = (canvas.width - PAD * 2) / 8;
    const board = view.pending ? withPending(frame.board, view.pending) : frame.board;

    ctx.fillStyle = "#3a2e24";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    for (let y = 0; y < 8; y++) {
      for (let x = 0; x < 8; x++) fillSquare(ctx, [x, y], (x + y) % 2 === 0 ? DARK : LIGHT);
    }
    ctx.font = "13px system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = "#eadfce";
    for (let i = 0; i < 8; i++) {
      const [cx] = center([i, 0]);
      const [, cy] = center([0, i]);
      ctx.fillText(FILES[i], cx, canvas.height - PAD / 2);
      ctx.fillText(String(i + 1), PAD / 2, cy);
    }

    const last = view.pending || frame.last;
    for (const name of [last?.slice(0, 2), last?.slice(2, 4)]) {
      if (parse(name)) fillSquare(ctx, parse(name), "rgba(246, 214, 70, 0.55)");
    }
    if (frame.check) fillSquare(ctx, parse(frame.check), "rgba(217, 48, 37, 0.6)");
    if (selected) fillSquare(ctx, parse(selected), "rgba(26, 115, 232, 0.45)");
    if (stuck && !view.pending) outline(ctx, parse(stuck), "rgba(60, 60, 60, 0.85)", 3, [CELL * 0.12, CELL * 0.08]);
    if (view.hover && !promoting) outline(ctx, parse(view.hover), "rgba(26, 115, 232, 0.8)", 3);

    for (let i = 0; i < 64; i++) {
      if (board[i] === ".") continue;
      const [cx, cy] = center([i % 8, Math.floor(i / 8)]);
      piece(ctx, board[i], cx, cy);
    }

    // 選了棋子之後，標出伺服器說可以走的格子：空格畫點，有子的格子畫圈
    for (const name of (selected && frame.legal?.[selected]) || []) {
      const [cx, cy] = center(parse(name));
      ctx.save();
      ctx.fillStyle = "rgba(20, 20, 20, 0.28)";
      ctx.strokeStyle = "rgba(20, 20, 20, 0.28)";
      ctx.lineWidth = CELL * 0.08;
      ctx.beginPath();
      if (pieceAt(board, name) === ".") {
        ctx.arc(cx, cy, CELL * 0.15, 0, Math.PI * 2);
        ctx.fill();
      } else {
        ctx.arc(cx, cy, CELL * 0.45, 0, Math.PI * 2);
        ctx.stroke();
      }
      ctx.restore();
    }

    if (view.marks) {
      arrow(ctx, view.marks.played, "#d93025", []);
      arrow(ctx, view.marks.best, "#1e8e3e", [CELL * 0.18, CELL * 0.12]);
    }
    if (view.spot && parse(view.spot)) outline(ctx, parse(view.spot), "#1a73e8", 4);

    // 升變：棋盤變暗，中間一排畫出后、車、象、馬讓玩家點；點別的地方就取消
    if (promoting) {
      ctx.fillStyle = "rgba(0, 0, 0, 0.55)";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      const white = frame.to_move === 1;
      PROMO.forEach((p, i) => {
        const px = PAD + (2 + i) * CELL, py = PAD + 3 * CELL;
        ctx.fillStyle = LIGHT;
        ctx.fillRect(px + 2, py + 2, CELL - 4, CELL - 4);
        piece(ctx, white ? p.toUpperCase() : p, px + CELL / 2, py + CELL / 2);
      });
      ctx.fillStyle = "#ffffff";
      ctx.font = "16px system-ui, sans-serif";
      ctx.fillText("兵升變：選一個棋子（點其他地方取消）", canvas.width / 2, PAD + 2.5 * CELL);
    }
  }

  // 滑鼠位置對應的格子名稱，棋盤外回傳 null
  function pointAt(canvas, event) {
    if (!CELL) return null;
    const rect = canvas.getBoundingClientRect();
    const px = (event.clientX - rect.left) * (canvas.width / rect.width);
    const py = (event.clientY - rect.top) * (canvas.height / rect.height);
    const c = Math.floor((px - PAD) / CELL);
    const r = Math.floor((py - PAD) / CELL);
    if (c < 0 || c > 7 || r < 0 || r > 7) return null;
    return flipped ? nameOf(7 - c, r) : nameOf(c, 7 - r);
  }

  // 滑鼠移到上面時要不要標示：能走的棋子，或選了棋子之後能去的格子
  function canPlay(frame, name) {
    if (promoting) return true;
    return Boolean(frame.legal?.[name]) || Boolean(selected && frame.legal?.[selected]?.includes(name));
  }

  // 點一下：第一下選棋子，第二下選目標格；湊成一步時回傳 UCI，否則回傳 null（app.js 的 GameBoard.select，和象棋同一個介面）
  function select(frame, name) {
    if (promoting) {
      const [c, r] = parse(name) ? screenOf(parse(name)) : [-1, -1];
      const pick = r === 3 && c >= 2 && c <= 5 ? PROMO[c - 2] : null;
      const action = pick ? promoting + pick : null;
      promoting = null;
      selected = null;
      return action;
    }
    stuck = null;
    if (selected && name && frame.legal?.[selected]?.includes(name)) {
      const move = selected + name;
      if (frame.promotions?.includes(move)) {
        promoting = move;
        return null;
      }
      selected = null;
      return move;
    }
    // 是不是自己的棋子只看大小寫（畫圖本來就用它分黑白）；能不能走完全照伺服器的 legal
    const ch = pieceAt(frame.board, name);
    if (ch !== "." && (ch === ch.toUpperCase()) === (frame.to_move === 1) && !frame.legal?.[name]) stuck = name;
    selected = name && frame.legal?.[name] && name !== selected ? name : null;
    return null;
  }

  const emptyFrame = () => START;

  // 走法的顯示文字：e2→e4、e7→e8 升變后
  function actionText(action) {
    if (action === "resign") return "認輸";
    if (!parse(action) || !parse(action.slice(2))) return action;
    const promo = action.length === 5 ? ` 升變${PROMO_NAMES[action[4]] ?? ""}` : "";
    return `${action.slice(0, 2)}→${action.slice(2, 4)}${promo}`;
  }

  const turnHint = (frame) => [
    frame.check ? "你被將軍了，這一步一定要解開。" : "",
    stuck && frame === shownFrame ? "這顆棋子現在沒有可以走的位置：可能被擋住，或是走了會讓自己的王被將軍。請換一顆。" : "",
  ].join("");

  return {
    draw, pointAt, canPlay, select, emptyFrame, actionText, turnHint,
    extraActions: [{ action: "resign", label: "認輸", confirm: "確定要認輸嗎？" }],
  };
})();
