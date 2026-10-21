"use strict";

// 德州撲克牌桌元件：只負責畫牌桌與產生動作按鈕，對局流程在 arena/app.js。
// frame 由伺服器提供，只含玩家看得到的牌：seats[].cards 是 ["As", "Kd"]（看得到）、[null, null]（背面）或 []（沒有牌）；
// legal 只在輪到玩家時才有（to_call、check、call、raise: [最少, 最多]、allin、pot、bet、current、bb）。
window.GameBoard = (() => {
  const SUIT = { s: "♠", h: "♥", d: "♦", c: "♣" };
  const SUIT_CODE = { "♠": "s", "♥": "h", "♦": "d", "♣": "c" };
  const RED = new Set(["h", "d"]);
  const W = 640;
  const H = 540; // 牌桌最下面的座位到 y≈505 為止；比棋盤矮，動作按鈕才不會被擠到視窗外
  const CENTER = [320, 290];
  const RADIUS = [255, 185];

  const rankText = (card) => (card[0] === "T" ? "10" : card[0]);
  // 說明中的「A♠」「10♦」→ 內部記法 "As"、"Td"
  const codeOf = (text) => (text ? (text.startsWith("10") ? "T" : text[0]) + SUIT_CODE[text.slice(-1)] : null);

  function actionText(action) {
    if (!action) return "";
    if (action.startsWith("raise ")) return `加注到 ${action.split(" ")[1]}`;
    return { fold: "棄牌", check: "過牌", call: "跟注", allin: "All in", deal: "下一手", leave: "離場" }[action] ?? action;
  }

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function card(ctx, x, y, code, highlight, scale = 1) {
    const w = 40 * scale;
    const h = 56 * scale;
    roundRect(ctx, x, y, w, h, 5);
    if (code) {
      ctx.fillStyle = "#ffffff";
      ctx.fill();
      ctx.strokeStyle = "#888888";
      ctx.lineWidth = 1;
      ctx.stroke();
      ctx.fillStyle = RED.has(code[1]) ? "#d93025" : "#111111";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.font = `bold ${Math.round(17 * scale)}px system-ui, sans-serif`;
      ctx.fillText(rankText(code), x + w / 2, y + h * 0.33);
      ctx.font = `${Math.round(20 * scale)}px system-ui, sans-serif`;
      ctx.fillText(SUIT[code[1]], x + w / 2, y + h * 0.72);
    } else {
      ctx.fillStyle = "#3b5bdb";
      ctx.fill();
      ctx.strokeStyle = "#dbe4ff";
      ctx.lineWidth = 2;
      ctx.stroke();
    }
    if (highlight) {
      ctx.save();
      ctx.strokeStyle = "#1a73e8";
      ctx.lineWidth = 4;
      roundRect(ctx, x - 4, y - 4, w + 8, h + 8, 7);
      ctx.stroke();
      ctx.restore();
    }
  }

  function seatPosition(i, n) {
    const angle = Math.PI / 2 + (i * 2 * Math.PI) / n; // 1 號位（玩家）在正下方，順時針排列
    return [CENTER[0] + RADIUS[0] * Math.cos(angle), CENTER[1] + RADIUS[1] * Math.sin(angle)];
  }

  // ---- 動畫 ----
  // 換到新的 frame（AI 行動、發牌、復盤逐步翻）時播一小段動畫：新的公共牌翻開、下注的籌碼滑向底池、
  // 剛行動的座位閃一下、贏家發亮並把底池收過去。只改畫法，局面一律是伺服器給的 frame。
  // 一步之內的順序：剛行動的座位閃一下 → 籌碼滑進底池 → 公共牌翻開（翻牌三張連著翻，轉牌、河牌各隔一下）→ 贏家發亮、收底池
  const FLIP_MS = 320; // 一張公共牌翻開
  const DEAL_GAP_MS = 90; // 翻牌的三張之間
  const STREET_GAP_MS = 380; // 一次發完好幾條街（例如 All in 後直接發完）時，轉牌、河牌之間停一下
  const CHIP_MS = 420; // 籌碼滑向底池
  const FLASH_MS = 700; // 剛行動的座位閃一下
  const WIN_MS = 1300; // 贏家發亮、底池滑過去
  const REDUCED = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches; // 系統要求減少動態時不播
  let shown = null; // 正在顯示的 frame
  let before = null; // 上一個 frame，用來比較哪裡變了；換了一手牌或往回翻就不比較
  let plan = null; // timeline(before, shown)
  let since = 0; // 換到 shown 的時間
  let pending = 0; // requestAnimationFrame 的編號
  let lastCall = null; // 最近一次 draw 的參數，動畫進行中用來重畫

  const ease = (x) => 1 - (1 - Math.min(1, Math.max(0, x))) ** 3;

  // 同一手牌往下一步（不是往回翻）才播動畫
  const forward = (prev, next) => prev && next.seats?.length && prev.hand === next.hand
    && next.board.length >= prev.board.length
    && next.seats.every((s) => s.total >= (prev.seats.find((o) => o.seat === s.seat)?.total ?? 0));

  // 這一步的時間表：哪些座位的籌碼要滑進底池、每張新公共牌什麼時候開始翻、翻完的時間
  function timeline(prev, next) {
    const closed = prev.street !== next.street || (next.hand_over && !prev.hand_over); // 這一輪下注結束，注收進底池
    const chips = [];
    for (const s of next.seats) {
      const old = prev.seats.find((o) => o.seat === s.seat);
      if (!old) continue;
      const added = s.total - old.total;
      const amount = closed ? old.bet + added : added;
      if (amount > 0) chips.push({ seat: s.seat, amount });
    }
    const flips = [];
    let at = chips.length ? CHIP_MS : 0;
    for (let i = prev.board.length; i < next.board.length; i++) {
      if (i > prev.board.length) at += i <= 2 ? DEAL_GAP_MS : STREET_GAP_MS;
      flips.push({ index: i, start: at });
    }
    return { chips, flips, end: flips.length ? at + FLIP_MS : chips.length ? CHIP_MS : 0 };
  }

  // 給 app.js：這一步的動畫要多久，下一個 AI 行動要等它播完才出現
  const stepDelay = (prev, next) => (forward(prev, next) && !REDUCED ? timeline(prev, next).end : 0);

  function draw(canvas, view) {
    if (view.frame !== shown) {
      before = forward(shown, view.frame) && !REDUCED ? shown : null;
      plan = before ? timeline(before, view.frame) : null;
      shown = view.frame;
      since = performance.now();
    }
    lastCall = [canvas, view];
    const elapsed = before ? performance.now() - since : Infinity;
    render(canvas, view, elapsed);
    const until = plan ? Math.max(FLASH_MS, plan.end + (view.frame.hand_over ? WIN_MS : 0)) : 0;
    if (elapsed < until + 50 && !pending) {
      pending = requestAnimationFrame(() => {
        pending = 0;
        draw(...lastCall);
      });
    }
  }

  // 公共牌翻開：先是背面變窄，再翻成正面，同時從上方落下一點
  function flipCard(ctx, x, y, code, highlight, progress) {
    if (progress >= 1) {
      card(ctx, x, y, code, highlight);
      return;
    }
    if (progress <= 0) return;
    const t = ease(progress);
    ctx.save();
    ctx.translate(x + 20, y + 28 - 14 * (1 - t));
    ctx.scale(Math.max(0.05, Math.abs(1 - 2 * t)), 1);
    card(ctx, -20, -28, t >= 0.5 ? code : null, false);
    ctx.restore();
  }

  function chip(ctx, x, y, amount, alpha) {
    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.beginPath();
    ctx.arc(x, y, 9, 0, Math.PI * 2);
    ctx.fillStyle = "#fbbc04";
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = "#b06000";
    ctx.stroke();
    ctx.fillStyle = "#ffffff";
    ctx.font = "bold 13px system-ui, sans-serif";
    ctx.textAlign = "left";
    ctx.fillText(String(amount), x + 13, y + 1);
    ctx.restore();
  }

  function ring(ctx, x, y, w, h, grow, color, alpha, width = 4) {
    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.lineWidth = width;
    ctx.strokeStyle = color;
    roundRect(ctx, x - w / 2 - grow, y - h / 2 - grow, w + grow * 2, h + grow * 2, 10 + grow);
    ctx.stroke();
    ctx.restore();
  }

  function render(canvas, view, elapsed) {
    const { frame } = view;
    if (canvas.height !== H) canvas.height = H; // 改高度會清空畫布，只在不同時才設
    const ctx = canvas.getContext("2d");
    const spot = codeOf(view.spot);
    ctx.fillStyle = "#1f2a24";
    ctx.fillRect(0, 0, W, H);
    ctx.beginPath();
    ctx.ellipse(CENTER[0], CENTER[1], RADIUS[0] - 30, RADIUS[1] - 30, 0, 0, Math.PI * 2);
    ctx.fillStyle = "#1e7a46";
    ctx.fill();
    ctx.lineWidth = 10;
    ctx.strokeStyle = "#6b4423";
    ctx.stroke();

    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = "#f1f3f4";
    if (!frame.seats?.length) {
      ctx.font = "18px system-ui, sans-serif";
      ctx.fillText("發牌中…", CENTER[0], CENTER[1]);
      return;
    }
    ctx.font = "15px system-ui, sans-serif";
    ctx.fillText(`第 ${frame.hand} 手牌　${frame.street}　底池 ${frame.pot}`, CENTER[0], CENTER[1] - 58);
    for (let i = 0; i < 5; i++) {
      const code = frame.board[i];
      const x = CENTER[0] - 2.5 * 46 + i * 46 + 3;
      const flip = plan?.flips.find((f) => f.index === i);
      const progress = flip ? (elapsed - flip.start) / FLIP_MS : 1;
      if (!code || progress <= 0) {
        roundRect(ctx, x, CENTER[1] - 36, 40, 56, 5);
        ctx.strokeStyle = "rgba(255,255,255,0.25)";
        ctx.lineWidth = 1;
        ctx.stroke();
      }
      if (code) flipCard(ctx, x, CENTER[1] - 36, code, code === spot, progress);
    }

    // 座位：底牌一律放在自己座位框的正上方，不伸進牌桌中間，才不會擋到公共牌與底池
    const n = frame.seats.length;
    const [bw, bh] = n > 6 ? [124, 52] : [132, 60];
    const places = {};
    const won = frame.hand_over && !(before && before.hand_over); // 這一步剛結束這手牌
    frame.seats.forEach((s, i) => {
      const [x, y] = seatPosition(i, n);
      places[s.seat] = [x, y];
      const old = before?.seats.find((o) => o.seat === s.seat);
      const turn = frame.to_move === s.seat && !frame.hand_over;
      const fading = s.folded && old && !old.folded ? 1 - 0.4 * ease(elapsed / 300) : s.folded ? 0.6 : 1;
      ctx.globalAlpha = s.out ? 0.35 : fading;
      roundRect(ctx, x - bw / 2, y - bh / 2, bw, bh, 10);
      ctx.fillStyle = turn ? "#fff4cc" : "#2d3436";
      ctx.fill();
      ctx.lineWidth = turn ? 3 : 1;
      ctx.strokeStyle = s.won ? "#fbbc04" : turn ? "#f9ab00" : "#555555";
      ctx.stroke();
      ctx.fillStyle = turn ? "#202124" : "#f1f3f4";
      const size = n > 6 ? 12 : 13;
      const line = n > 6 ? 14 : 16;
      ctx.font = `bold ${size}px system-ui, sans-serif`;
      ctx.fillText([s.name, s.role].filter(Boolean).join("　"), x, y - line, bw - 8);
      ctx.font = `${size}px system-ui, sans-serif`;
      ctx.fillText(s.out ? "籌碼輸光" : `籌碼 ${s.stack}${s.allin ? "（All in）" : ""}`, x, y, bw - 8);
      // 狀態列：這一輪的動作（過牌、跟注、加注…）；這一輪還沒動作就寫這手牌已經下注的總額；All in 的人一律寫 All in。
      // 攤牌時亮出的別人的牌，直接寫牌型
      const shownHand = s.seat !== 1 && s.hand_name ? s.hand_name : "";
      let status = s.total ? `已下注 ${s.total}` : "";
      if (s.won) status = [`贏得 ${s.won}`, shownHand].filter(Boolean).join("・");
      else if (s.folded) status = "已棄牌";
      else if (shownHand) status = shownHand;
      else if (s.allin) status = `All in（共 ${s.total}）`;
      else if (s.last) status = s.last === "跟注" ? `跟注 ${s.bet}` : s.last;
      if (status) {
        ctx.fillStyle = s.won ? "#e37400" : turn ? "#5f6368" : "#fde68a";
        ctx.fillText(status, x, y + line, bw - 8);
      }
      const scale = s.seat === 1 ? 1 : n > 6 ? 0.6 : 0.75; // 別人的牌畫小一點
      const [w, h] = [40 * scale, 56 * scale];
      const top = y - bh / 2 - 3 - h;
      s.cards.forEach((code, k) => card(ctx, x - w - 2 + k * (w + 4), top, code, code && code === spot, scale));
      ctx.globalAlpha = 1;
      if (s.seat === 1 && s.hand_name) {
        // 自己目前的牌型：寫在自己的底牌上方
        const label = `目前：${s.hand_name}`;
        ctx.font = "bold 13px system-ui, sans-serif";
        const tw = ctx.measureText(label).width + 16;
        roundRect(ctx, x - tw / 2, top - 25, tw, 22, 11);
        ctx.fillStyle = "#fde68a";
        ctx.fill();
        ctx.fillStyle = "#202124";
        ctx.fillText(label, x, top - 13);
      }
      if (s.button) {
        const [dx, dy] = [x + bw / 2 + 4, y - bh / 2 + 4];
        ctx.beginPath();
        ctx.arc(dx, dy, 11, 0, Math.PI * 2);
        ctx.fillStyle = "#ffffff";
        ctx.fill();
        ctx.fillStyle = "#202124";
        ctx.font = "bold 12px system-ui, sans-serif";
        ctx.fillText("D", dx, dy + 1);
      }
      // 剛行動的座位閃一下
      if (frame.last_seat === s.seat && elapsed < FLASH_MS) {
        ring(ctx, x, y, bw, bh, 3, "#fbbc04", 1 - elapsed / FLASH_MS);
      }
      // 贏家發亮：公共牌翻完之後，兩圈往外擴散的光
      if (won && s.won) {
        for (let k = 0; k < 2; k++) {
          const p = (elapsed - (plan?.end ?? 0) - k * 400) / 800;
          if (p > 0 && p < 1) ring(ctx, x, y, bw, bh, 4 + 14 * ease(p), "#fbbc04", 1 - p, 3);
        }
      }
    });

    // 籌碼動畫畫在最上層：這一步下的注（換街時連同上一輪的注）先滑進底池；公共牌翻完後，底池滑向贏家
    if (!plan) return;
    const pot = [CENTER[0], CENTER[1] - 58];
    for (const { seat, amount } of plan.chips) {
      const p = elapsed / CHIP_MS;
      if (p < 1) {
        const [x, y] = places[seat];
        const t = ease(p);
        chip(ctx, x + (pot[0] - x) * t * 0.85, y + (pot[1] - y) * t * 0.85, amount, 1 - 0.5 * t);
      }
    }
    if (won) {
      const p = (elapsed - plan.end - 250) / (WIN_MS - 500); // 先停一下讓人看到攤牌，再把底池收給贏家
      if (p > 0 && p < 1) {
        const t = ease(p);
        for (const s of frame.seats.filter((x) => x.won)) {
          const [x, y] = places[s.seat];
          chip(ctx, pot[0] + (x - pot[0]) * t, pot[1] + (y - pot[1]) * t, s.won, 1);
        }
      }
    }
  }

  function button(label, onClick, enabled, className = "") {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    b.className = className;
    b.disabled = !enabled;
    b.addEventListener("click", onClick);
    return b;
  }

  // 依局面產生動作按鈕。第一排是決定：棄牌（紅）、過牌或跟注（藍）、加注到 X、All in；
  // 第二排是加注金額：最小、半個底池、一個底池或自己輸入（按 Enter 直接加注）。一手結束後只有「下一手」。
  function renderControls(frame, container, enabled, playAction) {
    container.replaceChildren();
    if (!frame || frame.result) return;
    // 離場（使用者要求）：一手牌之間直接結束；一手牌進行中等於先棄牌。放在最右邊，免得誤按
    const leave = button("離場", () => {
      const ask = frame.hand_over ? "確定要離場嗎？會帶走目前的籌碼，結束這場牌局。"
        : "確定要離場嗎？這手牌會直接棄牌，已經下的注不會退回。";
      if (window.confirm(ask)) playAction("leave");
    }, enabled, "leave");
    if (frame.hand_over) {
      if (frame.to_move === 1) container.append(button("下一手", () => playAction("deal"), enabled, "primary"), leave);
      return;
    }
    const legal = frame.legal;
    if (!legal) return;
    if (legal.call) container.append(button("棄牌", () => playAction("fold"), enabled, "fold"));
    container.append(legal.check
      ? button("過牌", () => playAction("check"), enabled, "call")
      : button(`跟注 ${legal.to_call}`, () => playAction("call"), enabled, "call"));
    let sizes = null;
    if (legal.raise) {
      const [low, high] = legal.raise;
      const clamp = (v) => Math.min(high, Math.max(low, Math.round(v / 10) * 10));
      const potSize = (f) => clamp(legal.current + f * (legal.pot + legal.to_call));
      const input = document.createElement("input");
      Object.assign(input, { type: "number", min: String(low), max: String(high), step: "10", value: String(low) });
      input.className = "raise-amount";
      input.disabled = !enabled;
      input.setAttribute("aria-label", "加注到的金額");
      const amount = () => clamp(Number(input.value) || low);
      const commit = () => playAction(amount() >= high ? "allin" : `raise ${amount()}`);
      const raise = button("", commit, enabled);
      const show = () => { raise.textContent = `加注到 ${amount()}`; };
      input.addEventListener("input", show);
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !input.disabled) commit();
      });
      show();
      container.append(raise);
      sizes = document.createElement("div");
      sizes.className = "raise-sizes";
      for (const [label, value] of [["最小", low], ["½ 底池", potSize(0.5)], ["1 底池", potSize(1)]]) {
        sizes.append(button(label, () => { input.value = String(value); show(); }, enabled));
      }
      sizes.append(input);
    }
    if (legal.allin) {
      container.append(button("All in", () => {
        if (window.confirm("確定要 All in 嗎？")) playAction("allin");
      }, enabled));
    }
    container.append(leave);
    if (sizes) container.append(sizes);
  }

  // 勝率表用：玩家目前起手牌的代號，例如 AA、AKs（同花）、AKo（不同花）
  const ORDER = "23456789TJQKA";
  function referenceKey(frame) {
    const cards = frame?.seats?.find((s) => s.seat === 1)?.cards;
    if (!cards || cards.length !== 2 || cards.includes(null)) return null;
    const [a, b] = [...cards].sort((x, y) => ORDER.indexOf(y[0]) - ORDER.indexOf(x[0]));
    if (a[0] === b[0]) return a[0] + b[0];
    return a[0] + b[0] + (a[1] === b[1] ? "s" : "o");
  }

  const turnHint = (frame) => {
    if (frame.hand_over) return `${frame.hand_result}按「下一手」繼續，或按「離場」結束牌局。`;
    const mine = frame.seats?.find((s) => s.seat === 1)?.hand_name;
    const parts = [frame.legal?.call ? `要跟注 ${frame.legal.to_call}（底池 ${frame.pot}）` : "", mine ? `你目前是${mine}` : ""];
    return parts.filter(Boolean).join("；");
  };

  return {
    draw,
    pointAt: () => null, // 撲克不用點牌桌，動作都在按鈕
    canPlay: () => false,
    emptyFrame: () => ({ seats: [], board: [], pot: 0, to_move: 0, hand_over: false }),
    actionText,
    turnHint,
    stepDelay,
    referenceKey,
    renderControls,
    extraActions: [],
  };
})();
