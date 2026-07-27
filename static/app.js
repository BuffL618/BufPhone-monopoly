const app = document.querySelector("#app");
const modal = document.querySelector("#modal");
const modalTitle = document.querySelector("#modalTitle");
const modalBody = document.querySelector("#modalBody");

let room = null;
let config = null;
let events = null;
let toastTimer = null;
let pendingPassStartPlayerId = "";
let pendingPassStartTimer = null;
let pendingUndoLogId = "";
let pendingUndoTimer = null;
let propertySortMode = "player";
let propertyViewMode = "detail";
let auditTab = "count";

const qs = (selector, root = document) => root.querySelector(selector);
const qsa = (selector, root = document) => [...root.querySelectorAll(selector)];
const APP_NAME = "BufPhone | 手机大富翁记分器";
const DEFAULT_COLORS = [
  { name: "深红", value: "#8B1E2D" },
  { name: "深蓝", value: "#1D4E89" },
  { name: "深黄", value: "#9A6A00" },
  { name: "深绿", value: "#1F6F50" },
  { name: "浅红", value: "#F4A3A3" },
  { name: "浅蓝", value: "#9DC8F6" },
  { name: "浅黄", value: "#F4D76B" },
  { name: "浅绿", value: "#9BD8B2" },
];

function formatMoney(value) {
  return Number(value || 0).toLocaleString("zh-CN");
}

function assetTotal(player) {
  return player.properties.reduce((sum, prop) => sum + Number(prop.assetValue || 0), 0);
}

function netWorth(player) {
  return Number(player.cash || 0) + assetTotal(player);
}

function rankedPlayers(players) {
  let previousScore = null;
  let previousRank = 0;
  return [...players]
    .sort((a, b) => netWorth(b) - netWorth(a))
    .map((player, index) => {
      const score = netWorth(player);
      const rank = score === previousScore ? previousRank : index + 1;
      previousScore = score;
      previousRank = rank;
      return { player, rank, score };
    });
}

function playerColor(player) {
  return player?.color || "#65716d";
}

function colorStyle(player) {
  return `style="--player-color:${escapeHtml(playerColor(player))}"`;
}

function buildings(prop) {
  return prop.buildings || [];
}

function greenHouses(prop) {
  return buildings(prop).filter((building) => building.type === "house");
}

function hotelBuilding(prop) {
  return buildings(prop).find((building) => building.type === "hotel");
}

function hasBuildings(prop) {
  return buildings(prop).length > 0;
}

function canBuild(prop) {
  return !prop.mortgaged && !hotelBuilding(prop);
}

function mortgageRedeemCost(prop) {
  const value = Math.trunc(Number(prop.mortgageValue || prop.assetValue || 0));
  return Math.floor((value * 110 + 99) / 100);
}

function buildingBonusCount(prop) {
  return hotelBuilding(prop) ? 5 : greenHouses(prop).length;
}

function colorSetAmount(prop) {
  return Number(prop.colorSetAmount || 0);
}

function rentTotal(prop, baseAmount = Number(prop.toll || 0)) {
  return Number(baseAmount || 0) + colorSetAmount(prop) * buildingBonusCount(prop);
}

function colorSetExtra(prop) {
  return colorSetAmount(prop) * buildingBonusCount(prop);
}

function propertyRentValue(prop) {
  return rentTotal(prop);
}

function renderBuildingMarkers(prop) {
  const hotel = hotelBuilding(prop);
  const houses = greenHouses(prop);
  if (!hotel && !houses.length) return "";
  return `
    <div class="building-markers" aria-label="建筑">
      ${hotel ? `<span class="hotel-marker" title="红色房子"></span>` : houses.map(() => `<span class="house-marker" title="绿色房子"></span>`).join("")}
    </div>
  `;
}

function propertyOwner(propertyId) {
  for (const player of room.players || []) {
    const prop = player.properties.find((item) => item.id === propertyId);
    if (prop) return { player, prop };
  }
  return null;
}

function roomIdFromPath() {
  const match = location.pathname.match(/^\/room\/([^/]+)/);
  return match ? match[1] : null;
}

function showToast(message) {
  let toast = qs(".toast");
  if (!toast) {
    toast = document.createElement("div");
    toast.className = "toast";
    document.body.appendChild(toast);
  }
  toast.textContent = message;
  toast.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.add("hidden"), 2600);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || "操作失败");
  }
  return data;
}

async function postAction(type, payload = {}) {
  if (!room) return;
  try {
    const identity = getIdentity();
    const data = await api(`/api/rooms/${room.id}/actions`, {
      method: "POST",
      body: JSON.stringify({
        type,
        actorPlayerId: identity?.playerId,
        actorToken: identity?.token,
        ...payload,
      }),
    });
    room = data.room;
    render();
    return true;
  } catch (error) {
    showToast(error.message);
    return false;
  }
}

function identityKey() {
  return room ? `bufphone:${room.id}:identity` : "";
}

function getIdentity() {
  if (!room) return null;
  try {
    const raw = localStorage.getItem(identityKey());
    if (!raw) return null;
    const identity = JSON.parse(raw);
    if (!identity?.playerId || !identity?.token) return null;
    if (!room.players?.some((player) => player.id === identity.playerId)) {
      clearIdentity();
      return null;
    }
    return identity;
  } catch {
    clearIdentity();
    return null;
  }
}

function setIdentity(playerId, token) {
  localStorage.setItem(identityKey(), JSON.stringify({ playerId, token }));
}

function clearIdentity() {
  if (room) localStorage.removeItem(identityKey());
}

function myPlayer() {
  const identity = getIdentity();
  return identity ? getPlayer(identity.playerId) : null;
}

function isMine(playerId) {
  return getIdentity()?.playerId === playerId;
}

function requireMine(playerId) {
  if (isMine(playerId)) return true;
  showToast("这台手机只能操作自己的玩家");
  return false;
}

async function claimPlayer(playerId) {
  if (!room) return;
  try {
    const existing = getIdentity();
    const data = await api(`/api/rooms/${room.id}/claim`, {
      method: "POST",
      body: JSON.stringify({
        playerId,
        token: existing?.playerId === playerId ? existing.token : "",
      }),
    });
    room = data.room;
    setIdentity(data.playerId, data.token);
    render();
  } catch (error) {
    showToast(error.message);
  }
}

async function releaseIdentity() {
  const identity = getIdentity();
  if (!room || !identity) {
    clearIdentity();
    render();
    return;
  }
  try {
    const data = await api(`/api/rooms/${room.id}/release`, {
      method: "POST",
      body: JSON.stringify(identity),
    });
    room = data.room;
  } catch (error) {
    showToast(error.message);
  } finally {
    clearIdentity();
    render();
  }
}

async function loadConfig() {
  config = await api("/api/config");
}

async function createRoom() {
  try {
    const data = await api("/api/rooms", { method: "POST", body: "{}" });
    room = data.room;
    history.replaceState(null, "", `/room/${room.id}`);
    connectEvents();
    render();
  } catch (error) {
    showToast(error.message);
  }
}

async function loadRoom(id) {
  try {
    const data = await api(`/api/rooms/${id}`);
    room = data.room;
    connectEvents();
  } catch (error) {
    room = null;
    showToast(error.message);
  }
  render();
}

function connectEvents() {
  if (!room) return;
  if (events) events.close();
  events = new EventSource(`/api/rooms/${room.id}/events`);
  events.addEventListener("state", (event) => {
    const data = JSON.parse(event.data);
    room = data.room;
    render();
  });
  events.onerror = () => {
    showToast("实时同步断开，正在等待浏览器重连");
  };
}

function pageShell(content) {
  return `
    <main class="app-shell">
      <div class="topbar">
        <div class="brand">
          <h1>${APP_NAME}</h1>
          <p>${room ? `房间 ${room.id}` : "现金、房产、过路费实时记账"}</p>
        </div>
        <div class="top-actions">
          ${room?.started ? `<button class="top-chip" data-action="show-ranking">排名</button>` : ""}
          ${room?.started ? `<button class="top-chip" data-action="show-properties">房产</button>` : ""}
          ${room?.started ? `<button class="top-chip" data-action="show-audit">盘点</button>` : ""}
          ${room?.started ? `<button class="top-chip" data-action="show-log">记录</button>` : ""}
          ${room ? `<button class="top-chip secondary" data-action="qr">加入</button>` : ""}
        </div>
      </div>
      ${content}
    </main>
  `;
}

function renderHome() {
  app.innerHTML = pageShell(`
    <section class="hero">
      <div>
        <h2 class="intro-title">大富翁<br />现场记分</h2>
        <p class="intro-copy">创建房间后，其他玩家扫码加入。买房、升级、收租和抵押都会即时同步到所有手机。</p>
      </div>
      <div class="panel">
        <div class="panel-inner setup-grid">
          <button data-action="create-room">创建新房间</button>
          <label>
            输入房间号加入
            <input id="joinRoomInput" placeholder="例如 A7K9Q2" autocomplete="off" />
          </label>
          <button class="secondary" data-action="join-room">加入房间</button>
        </div>
      </div>
    </section>
  `);
}

function renderMissingRoom() {
  app.innerHTML = pageShell(`
    <section class="hero">
      <div class="panel">
        <div class="panel-inner setup-grid">
          <h2 class="intro-title">房间不存在</h2>
          <p class="intro-copy">请让创建者重新打开二维码，或创建一个新房间。</p>
          <button data-action="create-room">创建新房间</button>
        </div>
      </div>
    </section>
  `);
}

function renderSetup() {
  const rows = Array.from({ length: 4 }, (_, index) => {
    const name = `玩家${index + 1}`;
    return setupPlayerRow(name);
  }).join("");

  app.innerHTML = pageShell(`
    <section class="panel">
      <div class="panel-inner setup-grid">
        <div>
          <h2 class="intro-title">开局设置</h2>
          <p class="intro-copy">输入所有玩家名字和统一初始资金，开始后最多支持 8 人同局操作。</p>
        </div>
        <label>
          每人初始资金
          <input id="initialCash" type="number" inputmode="numeric" min="0" step="1" value="15000" />
        </label>
        <label>
          经过起点金额
          <input id="passStartAmount" type="number" inputmode="numeric" min="0" step="1" value="2000" />
        </label>
        <div class="setup-list" id="setupPlayers">${rows}</div>
        <div class="actions-row">
          <button class="secondary" data-action="add-setup-player">增加玩家</button>
          <button data-action="start-game">开始游戏</button>
        </div>
      </div>
    </section>
  `);
}

function setupPlayerRow(name = "") {
  return `
    <div class="setup-player-row">
      <input class="setup-player-name" value="${escapeHtml(name)}" maxlength="18" />
      <button class="ghost icon-button" data-action="remove-setup-player" title="移除">×</button>
    </div>
  `;
}

function renderGame() {
  const players = room.players || [];
  const debtPlayers = players.filter((player) => Number(player.cash || 0) < 0);
  const me = myPlayer();

  app.innerHTML = pageShell(`
    <section class="game-grid">
      ${renderCashBoard(players, me?.id)}
      ${debtPlayers.length ? `<div class="wide debt-note panel">有玩家现金为负，需要选择房产抵押或进行现金调整。</div>` : ""}
      ${me ? renderSelfArea(me) : renderIdentityPicker(players)}
    </section>
  `);
}

function renderCashBoard(players, myPlayerId = "") {
  const leaders = rankedPlayers(players).filter((item) => item.rank === 1).map((item) => item.player.id);
  return `
    <section class="cash-board section">
      <div class="section-head">
        <h2>现金情况</h2>
      </div>
      <div class="cash-list">
        ${players.map((player) => `
          <div class="cash-row ${player.id === myPlayerId ? "mine" : ""} ${leaders.includes(player.id) ? "leader" : ""} ${Number(player.cash || 0) <= 0 ? "bust" : ""}" ${colorStyle(player)}>
            <div>
              <strong>${escapeHtml(player.name)}</strong>
              ${leaders.includes(player.id) ? `<span class="leader-badge">Top1</span>` : ""}
              ${player.id === myPlayerId ? `<span>我</span>` : ""}
            </div>
            <em class="${player.cash < 0 ? "cash-negative" : ""}">${formatMoney(player.cash)}</em>
            ${Number(player.cash || 0) <= 0 ? `<small>立刻卖房或抵押</small>` : ""}
          </div>
        `).join("")}
      </div>
    </section>
  `;
}

function renderSelfArea(player) {
  return `
    <section class="section">
      <div class="section-head">
        <h2>我的操作</h2>
        <div class="section-actions">
          <button class="secondary mini-button" data-action="switch-player">切换</button>
        </div>
      </div>
      ${renderPlayerCard(player)}
    </section>
  `;
}

function renderIdentityPicker(players) {
  return `
    <section class="panel">
      <div class="panel-inner setup-grid">
        <div>
          <h2 class="compact-title">选择你的玩家</h2>
          <p class="intro-copy">这台手机之后只显示并操作所选玩家。</p>
        </div>
        <div class="identity-list">
          ${players.map((player) => `
            <button
              class="identity-button ${player.claimed ? "claimed" : ""}"
              data-action="claim-player"
              data-player-id="${player.id}"
              ${player.claimed ? "disabled" : ""}
            >
              <span>${escapeHtml(player.name)}</span>
              <em>${player.claimed ? "已绑定" : `现金 ${formatMoney(player.cash)}`}</em>
            </button>
          `).join("")}
        </div>
      </div>
    </section>
  `;
}

function renderPlayerCard(player) {
  const assets = assetTotal(player);
  const total = netWorth(player);
  const cashClass = player.cash < 0 ? "cash-negative" : "";
  const confirmPass = pendingPassStartPlayerId === player.id;
  const hasRentableProperty = player.properties.some((prop) => !prop.mortgaged);
  return `
    <article class="player-card" data-player-id="${player.id}">
      <div class="player-head">
        <div class="player-title">
          <strong>${escapeHtml(player.name)}</strong>
          <div class="player-title-actions">
            <button class="ghost mini-button" data-action="rename-player" data-player-id="${player.id}" title="改名">改名</button>
            <button class="ghost mini-button color-button" data-action="edit-color" data-player-id="${player.id}" ${colorStyle(player)} title="改颜色">
              <i></i>颜色
            </button>
          </div>
        </div>
        <div class="money-summary">
          <div class="cash-block">
            <span>现金</span>
            <strong class="${cashClass}">${formatMoney(player.cash)}</strong>
          </div>
          <div class="total-block">
            <span>现金 + 固定资产</span>
            <strong>${formatMoney(total)}</strong>
            <em>固定资产 ${formatMoney(assets)}</em>
          </div>
        </div>
      </div>
      ${player.cash < 0 ? `<div class="debt-note">现金已为负数。可抵押房产，抵押后立刻增加现金，房产固定资产改为抵押价值。</div>` : ""}
      <div class="player-actions">
        <button class="start-button ${confirmPass ? "confirm" : ""}" data-action="pass-start" data-player-id="${player.id}">
          ${confirmPass ? "确认" : "起点"} +${formatMoney(room.passStartAmount || 0)}
        </button>
        <button data-action="collect-rent" data-player-id="${player.id}" ${hasRentableProperty && room.players.length > 1 ? "" : "disabled"}>收钱</button>
        <button class="secondary" data-action="cash-adjust" data-player-id="${player.id}">自定义</button>
        <button class="blue" data-action="add-property" data-player-id="${player.id}">买地</button>
      </div>
      <div class="property-section-title">房产</div>
      <div class="property-list">
        ${player.properties.length ? player.properties.map((prop) => renderPropertyRow(player, prop)).join("") : `<div class="empty">暂无房产</div>`}
      </div>
    </article>
  `;
}

function renderPropertyRow(player, prop) {
  const hotel = hotelBuilding(prop);
  const houses = greenHouses(prop);
  const thirdAction = hasBuildings(prop)
    ? `<button class="warning" data-action="sell-building" data-player-id="${player.id}" data-property-id="${prop.id}">卖房</button>`
    : prop.mortgaged
      ? `<button class="warning" data-action="redeem-property" data-player-id="${player.id}" data-property-id="${prop.id}">赎回</button>`
      : `<button class="warning" data-action="mortgage-property" data-player-id="${player.id}" data-property-id="${prop.id}">抵押</button>`;
  return `
    <div class="property-row">
      <div class="property-main">
        <div class="property-title">
          <strong>${escapeHtml(prop.name)}</strong>
          <span>过路费 ${formatMoney(rentTotal(prop))} · 固定资产 ${formatMoney(prop.assetValue)}</span>
          ${colorSetAmount(prop) ? `<span class="color-set-line">基础 ${formatMoney(prop.toll)} + 同色 ${formatMoney(colorSetExtra(prop))}</span>` : ""}
          ${renderBuildingMarkers(prop)}
        </div>
        ${prop.mortgaged ? `<span class="badge red">已抵押</span>` : hotel ? `<span class="badge red">红房子</span>` : houses.length ? `<span class="badge">绿房子 ${houses.length}</span>` : `<span class="badge">土地</span>`}
      </div>
      <div class="property-actions">
        <button class="secondary" data-action="edit-property" data-player-id="${player.id}" data-property-id="${prop.id}">编辑</button>
        <button class="secondary color-set-button ${colorSetAmount(prop) ? "active" : ""}" data-action="color-set-property" data-player-id="${player.id}" data-property-id="${prop.id}">
          ${colorSetAmount(prop) ? `+${formatMoney(colorSetAmount(prop))}` : "同色"}
        </button>
        <button class="blue" data-action="upgrade-property" data-player-id="${player.id}" data-property-id="${prop.id}" ${canBuild(prop) ? "" : "disabled"}>建房子</button>
        ${thirdAction}
      </div>
    </div>
  `;
}

function renderRanking(players) {
  return `
    <section class="section wide">
      <div class="section-head">
        <h2>实时排名</h2>
      </div>
      ${renderRankingList(players)}
    </section>
  `;
}

function renderLog(log) {
  return `
    <section class="section wide">
      <div class="section-head">
        <h2>操作记录</h2>
      </div>
      ${renderLogList(log)}
    </section>
  `;
}

function renderRankingList(players) {
  const ranking = rankedPlayers(players);
  return `
    <div class="ranking-list">
      ${ranking.map(({ player, rank }) => `
        <div class="rank-card ${rank === 1 ? "leader" : ""}" ${colorStyle(player)}>
          <div class="rank-num">${rank === 1 ? "1" : rank}</div>
          <div class="rank-name">
            <strong>${escapeHtml(player.name)}</strong>
            <p>现金 ${formatMoney(player.cash)} · 固定资产 ${formatMoney(assetTotal(player))}</p>
          </div>
          <div class="rank-total">${formatMoney(netWorth(player))}</div>
        </div>
      `).join("")}
    </div>
  `;
}

function renderLogList(log) {
  const identity = getIdentity();
  return `
    <div class="log-list">
      ${log.length ? log.map((item) => `
        <div class="log-item ${item.undone ? "undone" : ""}">
          <div class="log-text">
            <time>${escapeHtml(item.time)}</time>${escapeHtml(item.text)}
            ${item.undone ? `<span class="log-state">已撤回</span>` : ""}
          </div>
          ${identity?.playerId === item.actorPlayerId && (item.undoable || item.restorable) ? `
            <div class="log-actions">
              ${item.undoable ? `
                <button
                  type="button"
                  class="secondary mini-button"
                  data-action="undo-log"
                  data-log-id="${item.id}"
                >${pendingUndoLogId === item.id ? "确认" : "撤回"}</button>
              ` : ""}
              ${item.restorable ? `
                <button
                  type="button"
                  class="secondary mini-button"
                  data-action="restore-log"
                  data-log-id="${item.id}"
                >恢复</button>
              ` : ""}
            </div>
          ` : ""}
        </div>
      `).join("") : `<div class="empty">暂无记录</div>`}
    </div>
  `;
}

function currentProperty(ownerId, propertyId) {
  return getPlayer(ownerId)?.properties.find((prop) => prop.id === propertyId);
}

function activeRentEvents() {
  return (room.rentEvents || []).filter((event) => !event.undone);
}

function rentGroups() {
  const groups = new Map();
  for (const event of activeRentEvents()) {
    const key = `${event.ownerId}:${event.propertyId}`;
    const currentOwner = getPlayer(event.ownerId);
    const currentProp = currentProperty(event.ownerId, event.propertyId);
    if (!groups.has(key)) {
      groups.set(key, {
        ownerId: event.ownerId,
        ownerName: currentOwner?.name || event.ownerName,
        propertyId: event.propertyId,
        propertyName: currentProp?.name || event.propertyName,
        count: 0,
        total: 0,
        max: 0,
      });
    }
    const group = groups.get(key);
    group.ownerName = currentOwner?.name || group.ownerName;
    group.propertyName = currentProp?.name || group.propertyName;
    group.count += 1;
    group.total += Number(event.amount || 0);
    group.max = Math.max(group.max, Number(event.amount || 0));
  }
  return [...groups.values()];
}

function renderAuditList() {
  const groups = rentGroups();
  const singleEvents = activeRentEvents().map((event) => {
    const currentOwner = getPlayer(event.ownerId);
    const currentProp = currentProperty(event.ownerId, event.propertyId);
    return {
      ...event,
      ownerName: currentOwner?.name || event.ownerName,
      propertyName: currentProp?.name || event.propertyName,
    };
  });

  if (!groups.length) {
    return `<div class="empty">还没有收钱记录</div>`;
  }

  const byCount = [...groups].sort((a, b) => b.count - a.count || b.total - a.total);
  const byTotal = [...groups].sort((a, b) => b.total - a.total || b.count - a.count);
  const bySingle = [...singleEvents].sort((a, b) => Number(b.amount || 0) - Number(a.amount || 0));
  const tabs = {
    count: {
      label: "次数",
      title: "路过被收钱次数最多",
      items: byCount,
      valueFn: (item) => `${item.count} 次`,
      detailFn: (item) => `累计 ${formatMoney(item.total)}`,
    },
    total: {
      label: "总额",
      title: "收钱总额度最大",
      items: byTotal,
      valueFn: (item) => formatMoney(item.total),
      detailFn: (item) => `${item.count} 次`,
    },
    single: {
      label: "单次",
      title: "单次收钱额度最大",
      items: bySingle,
      valueFn: (item) => formatMoney(item.amount),
      detailFn: (item) => item.payerName ? `付款 ${escapeHtml(item.payerName)}` : "",
    },
  };
  const tab = tabs[auditTab] || tabs.count;

  return `
    <div class="audit-list">
      <div class="segmented three">
        ${Object.entries(tabs).map(([key, item]) => `
          <button type="button" class="${auditTab === key ? "active" : ""}" data-action="switch-audit" data-audit-tab="${key}">${item.label}</button>
        `).join("")}
      </div>
      ${renderAuditSection(tab.title, tab.items, tab.valueFn, tab.detailFn)}
    </div>
  `;
}

function renderAuditSection(title, items, valueFn, detailFn) {
  return `
    <section class="audit-section">
      <h3>${title}</h3>
      <div class="audit-rows">
        ${items.map((item, index) => {
          const owner = getPlayer(item.ownerId);
          return `
            <div class="audit-row" ${colorStyle(owner)}>
              <div class="rank-num small">${index + 1}</div>
              <div class="audit-main">
                <strong>${escapeHtml(item.propertyName)}</strong>
                <span><i class="player-dot"></i>${escapeHtml(item.ownerName)} · ${detailFn(item)}</span>
              </div>
              <em>${valueFn(item)}</em>
            </div>
          `;
        }).join("")}
      </div>
    </section>
  `;
}

function allPropertyItems() {
  return (room.players || []).flatMap((player, playerIndex) =>
    player.properties.map((prop) => ({ player, prop, playerIndex })),
  );
}

function renderPropertyOverviewList(sortMode = propertySortMode) {
  const items = allPropertyItems();
  if (sortMode === "toll") {
    items.sort((a, b) => propertyRentValue(b.prop) - propertyRentValue(a.prop) || a.playerIndex - b.playerIndex);
  } else {
    items.sort((a, b) => a.playerIndex - b.playerIndex || propertyRentValue(b.prop) - propertyRentValue(a.prop));
  }

  return `
    <div class="property-overview">
      <div class="segmented">
        <button type="button" class="${sortMode === "player" ? "active" : ""}" data-action="sort-properties" data-sort="player">按玩家</button>
        <button type="button" class="${sortMode === "toll" ? "active" : ""}" data-action="sort-properties" data-sort="toll">按过路费</button>
      </div>
      <div class="segmented">
        <button type="button" class="${propertyViewMode === "detail" ? "active" : ""}" data-action="property-view" data-view="detail">详情</button>
        <button type="button" class="${propertyViewMode === "compact" ? "active" : ""}" data-action="property-view" data-view="compact">缩略</button>
      </div>
      <div class="${propertyViewMode === "compact" ? "property-compact-grid" : "property-overview-list"}">
        ${items.length ? items.map(({ player, prop }) => (
          propertyViewMode === "compact" ? renderCompactPropertyCard(player, prop) : renderOverviewPropertyCard(player, prop)
        )).join("") : `<div class="empty">暂无房产</div>`}
      </div>
    </div>
  `;
}

function renderOverviewPropertyCard(player, prop) {
  const hotel = hotelBuilding(prop);
  const houses = greenHouses(prop);
  return `
    <div class="overview-property-card" ${colorStyle(player)}>
      <div class="overview-property-head">
        <div>
          <strong>${escapeHtml(prop.name)}</strong>
          <span><i class="player-dot"></i>${escapeHtml(player.name)}</span>
        </div>
        ${prop.mortgaged ? `<span class="badge red">已抵押</span>` : hotel ? `<span class="badge red">红房子</span>` : houses.length ? `<span class="badge">绿房子 ${houses.length}</span>` : `<span class="badge">土地</span>`}
      </div>
      ${renderBuildingMarkers(prop)}
      <div class="overview-property-meta">
        <span>过路费 <strong>${formatMoney(rentTotal(prop))}</strong>${colorSetAmount(prop) ? `<em>基础 ${formatMoney(prop.toll)} + 同色 ${formatMoney(colorSetExtra(prop))}</em>` : ""}</span>
        <span>固定资产 <strong>${formatMoney(prop.assetValue)}</strong></span>
      </div>
    </div>
  `;
}

function renderCompactPropertyCard(player, prop) {
  const hotel = hotelBuilding(prop);
  const houses = greenHouses(prop);
  return `
    <div class="compact-property-card ${prop.mortgaged ? "mortgaged" : ""}" ${colorStyle(player)}>
      <div class="compact-property-top">
        <strong>${escapeHtml(prop.name)}</strong>
        ${hotel ? `<em class="compact-hotel">红</em>` : houses.length ? `<em class="compact-house">绿${houses.length}</em>` : ""}
      </div>
      <span>${formatMoney(rentTotal(prop))}</span>
      ${renderBuildingMarkers(prop)}
    </div>
  `;
}

function render() {
  if (!room && roomIdFromPath()) {
    renderMissingRoom();
  } else if (!room) {
    renderHome();
  } else if (!room.started) {
    renderSetup();
  } else {
    renderGame();
  }
}

function getPlayer(playerId) {
  return room.players.find((player) => player.id === playerId);
}

function getProperty(playerId, propertyId) {
  const player = getPlayer(playerId);
  return player?.properties.find((prop) => prop.id === propertyId);
}

function openModal(title, body, onSubmit) {
  modalTitle.textContent = title;
  modalBody.innerHTML = `<div class="modal-body">${body}</div>`;
  const form = qs(".modal-box", modal);
  form.onsubmit = async (event) => {
    event.preventDefault();
    const ok = await onSubmit(new FormData(form), form);
    if (ok !== false) {
      modal.close();
    }
  };
  if (!modal.open) modal.showModal();
}

function formActions(label = "确认") {
  return `
    <div class="form-actions">
      <button class="secondary" type="button" data-modal-close>取消</button>
      <button type="submit">${label}</button>
    </div>
  `;
}

function showAddProperty(playerId) {
  if (!requireMine(playerId)) return;
  const player = getPlayer(playerId);
  openModal(
    `${player.name} 买地`,
    `
      <div class="form-grid">
        <label>土地名称<input name="name" maxlength="28" required /></label>
        <label>购买金额<input name="cost" type="number" inputmode="numeric" min="0" step="1" required /></label>
        <label>过路费<input name="toll" type="number" inputmode="numeric" min="0" step="1" required /></label>
        ${formActions("买地")}
      </div>
    `,
    (data) => postAction("addProperty", {
      playerId,
      name: data.get("name"),
      cost: data.get("cost"),
      toll: data.get("toll"),
    }),
  );
}

function showEditProperty(playerId, propertyId) {
  if (!requireMine(playerId)) return;
  const player = getPlayer(playerId);
  const prop = getProperty(playerId, propertyId);
  openModal(
    `编辑 ${prop.name}`,
    `
      <div class="form-grid">
        <label>土地名称<input name="name" maxlength="28" value="${escapeHtml(prop.name)}" required /></label>
        <label>过路费<input name="toll" type="number" inputmode="numeric" min="0" step="1" value="${prop.toll}" required /></label>
        <label>同色集齐金额<input name="colorSetAmount" type="number" inputmode="numeric" min="0" step="1" value="${colorSetAmount(prop)}" /></label>
        <div class="form-actions three">
          <button class="secondary" type="button" data-modal-close>取消</button>
          <button class="danger" data-delete-property="${prop.id}" type="button">删除</button>
          <button type="submit">保存</button>
        </div>
      </div>
    `,
    (data) => postAction("updateProperty", {
      playerId,
      propertyId,
      name: data.get("name"),
      toll: data.get("toll"),
      colorSetAmount: data.get("colorSetAmount"),
    }),
  );
  qs("[data-delete-property]", modal).addEventListener("click", async () => {
    if (!confirm(`删除 ${player.name} 的 ${prop.name}？固定资产会直接移除。`)) return;
    const ok = await postAction("deleteProperty", { playerId, propertyId });
    if (ok) modal.close();
  });
}

function showColorSetProperty(playerId, propertyId) {
  if (!requireMine(playerId)) return;
  const prop = getProperty(playerId, propertyId);
  openModal(
    `${prop.name} 同色集齐`,
    `
      <div class="form-grid">
        <label>同色集齐金额<input name="colorSetAmount" type="number" inputmode="numeric" min="0" step="1" value="${colorSetAmount(prop)}" required /></label>
        <p class="intro-copy">收钱时按建筑数量加收：绿色房子按栋数，红色房子按 5 栋。填 0 可关闭。</p>
        ${formActions("保存")}
      </div>
    `,
    (data) => postAction("updateProperty", {
      playerId,
      propertyId,
      name: prop.name,
      toll: prop.toll,
      colorSetAmount: data.get("colorSetAmount"),
    }),
  );
}

function showUpgradeProperty(playerId, propertyId) {
  if (!requireMine(playerId)) return;
  const prop = getProperty(playerId, propertyId);
  if (!canBuild(prop)) {
    showToast(prop.mortgaged ? "土地已抵押，不能建房子" : "已有红色房子，不能继续建房子");
    return;
  }
  const nextLabel = greenHouses(prop).length >= 4 ? "红色房子" : "绿色房子";
  openModal(
    `给 ${prop.name} 建${nextLabel}`,
    `
      <div class="form-grid">
        <label>建房子金额<input name="cost" type="number" inputmode="numeric" min="1" step="1" required /></label>
        <label>建房子后过路费<input name="toll" type="number" inputmode="numeric" min="0" step="1" value="${prop.toll}" /></label>
        ${formActions("建房子")}
      </div>
    `,
    (data) => postAction("upgradeProperty", {
      playerId,
      propertyId,
      cost: data.get("cost"),
      toll: data.get("toll"),
    }),
  );
}

function showMortgage(playerId, propertyId = "") {
  if (!requireMine(playerId)) return;
  const player = getPlayer(playerId);
  const properties = player.properties.filter((prop) => !prop.mortgaged && !hasBuildings(prop));
  if (!properties.length) {
    showToast("没有可抵押土地；有房子的土地需要先卖房");
    return;
  }
  const options = properties.map((prop) => `
    <option value="${prop.id}" ${prop.id === propertyId ? "selected" : ""}>
      ${escapeHtml(prop.name)} · 当前固定资产 ${formatMoney(prop.assetValue)}
    </option>
  `).join("");
  openModal(
    `${player.name} 抵押房产`,
    `
      <div class="form-grid">
        <label>选择房产<select name="propertyId">${options}</select></label>
        <label>抵押价值<input name="amount" type="number" inputmode="numeric" min="0" step="1" required /></label>
        <p class="intro-copy">抵押后，玩家现金增加该价值；所选房产固定资产改为该抵押价值。</p>
        ${formActions("抵押")}
      </div>
    `,
    (data) => postAction("mortgageProperty", {
      playerId,
      propertyId: data.get("propertyId"),
      amount: data.get("amount"),
    }),
  );
}

function showRedeemProperty(playerId, propertyId) {
  if (!requireMine(playerId)) return;
  const prop = getProperty(playerId, propertyId);
  if (!prop?.mortgaged) {
    showToast("该土地未抵押");
    return;
  }
  const cost = mortgageRedeemCost(prop);
  openModal(
    `赎回 ${prop.name}`,
    `
      <div class="form-grid">
        <div class="form-static"><span>赎回价格</span><strong>${formatMoney(cost)}</strong></div>
        ${formActions("赎回")}
      </div>
    `,
    () => postAction("redeemProperty", { playerId, propertyId }),
  );
}

function showSellBuilding(playerId, propertyId) {
  if (!requireMine(playerId)) return;
  const prop = getProperty(playerId, propertyId);
  const hotel = hotelBuilding(prop);
  const houses = greenHouses(prop);
  if (!hotel && !houses.length) {
    showToast("该土地没有可卖的房子");
    return;
  }
  const soldCost = Number((hotel || houses[houses.length - 1]).cost || 0);
  const income = Math.floor(soldCost / 2);
  openModal(
    `卖出 ${prop.name} 的${hotel ? "红色房子" : "绿色房子"}`,
    `
      <div class="form-grid">
        <div class="form-static"><span>获得现金</span><strong>${formatMoney(income)}</strong></div>
        ${hotel ? `<p class="intro-copy">卖出红色房子后，会变回四栋绿色小房子。</p>` : ""}
        <label>卖房后过路费<input name="toll" type="number" inputmode="numeric" min="0" step="1" value="${prop.toll}" /></label>
        ${formActions("卖房")}
      </div>
    `,
    (data) => postAction("sellBuilding", {
      playerId,
      propertyId,
      toll: data.get("toll"),
    }),
  );
}

function showCashAdjust(playerId) {
  if (!requireMine(playerId)) return;
  const player = getPlayer(playerId);
  const cashValue = Math.abs(Math.trunc(Number(player.cash || 0)));
  const tenPercent = Math.floor((cashValue + 5) / 10);
  openModal(
    `${player.name} 自定义`,
    `
      <div class="form-grid">
        <div class="quick-adjust">
          <button type="button" class="secondary" data-quick-adjust="in" data-amount="${tenPercent}">+10% ${formatMoney(tenPercent)}</button>
          <button type="button" class="secondary" data-quick-adjust="out" data-amount="${tenPercent}">-10% ${formatMoney(tenPercent)}</button>
        </div>
        <label>类型
          <select name="direction" id="cashDirection">
            <option value="in">收入</option>
            <option value="out">支出</option>
          </select>
        </label>
        <label>金额<input id="cashAmount" name="amount" type="number" inputmode="numeric" min="1" step="1" required /></label>
        <label>备注<input name="note" maxlength="24" placeholder="经过起点、事件卡、罚款" /></label>
        ${formActions("记账")}
      </div>
    `,
    (data) => postAction("adjustCash", {
      playerId,
      direction: data.get("direction"),
      amount: data.get("amount"),
      note: data.get("note"),
    }),
  );
  qsa("[data-quick-adjust]", modal).forEach((button) => {
    button.addEventListener("click", () => {
      qs("#cashDirection", modal).value = button.dataset.quickAdjust;
      qs("#cashAmount", modal).value = button.dataset.amount;
    });
  });
}

async function passStart(playerId) {
  if (!requireMine(playerId)) return;
  if (pendingPassStartPlayerId !== playerId) {
    pendingPassStartPlayerId = playerId;
    clearTimeout(pendingPassStartTimer);
    pendingPassStartTimer = setTimeout(() => clearPendingPassStart(), 2800);
    render();
    return;
  }
  clearPendingPassStart(false);
  await postAction("passStart", { playerId });
}

function clearPendingPassStart(renderNow = true) {
  if (!pendingPassStartPlayerId) return false;
  pendingPassStartPlayerId = "";
  clearTimeout(pendingPassStartTimer);
  if (renderNow) render();
  return true;
}

function showRenamePlayer(playerId) {
  if (!requireMine(playerId)) return;
  const player = getPlayer(playerId);
  openModal(
    "修改玩家名称",
    `
      <div class="form-grid">
        <label>玩家名称<input name="name" maxlength="18" value="${escapeHtml(player.name)}" required /></label>
        ${formActions("保存")}
      </div>
    `,
    (data) => postAction("renamePlayer", {
      playerId,
      name: data.get("name"),
      color: playerColor(player),
    }),
  );
}

function showColorEditor(playerId) {
  if (!requireMine(playerId)) return;
  const player = getPlayer(playerId);
  const currentColor = playerColor(player).toUpperCase();
  openModal(
    "修改主题颜色",
    `
      <div class="form-grid">
        <div class="color-preview" ${colorStyle(player)}>
          <span></span>
          <strong>${escapeHtml(player.name)}</strong>
        </div>
        <div class="color-palette">
          ${DEFAULT_COLORS.map((color) => `
            <button
              type="button"
              class="color-choice ${color.value === currentColor ? "active" : ""}"
              data-palette-color="${color.value}"
              style="--choice-color:${color.value}"
            >
              <i></i><span>${color.name}</span>
            </button>
          `).join("")}
        </div>
        <label>RGB/RMG 编号<input id="colorInput" name="color" maxlength="24" value="${escapeHtml(playerColor(player))}" placeholder="#8B1E2D 或 139,30,45" required /></label>
        ${formActions("保存")}
      </div>
    `,
    (data) => postAction("renamePlayer", {
      playerId,
      name: player.name,
      color: data.get("color"),
    }),
  );
  const colorInput = qs("#colorInput", modal);
  const preview = qs(".color-preview", modal);
  qsa("[data-palette-color]", modal).forEach((button) => {
    button.addEventListener("click", () => {
      qsa("[data-palette-color]", modal).forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      colorInput.value = button.dataset.paletteColor;
      preview.style.setProperty("--player-color", button.dataset.paletteColor);
    });
  });
}

function showCollectRent(receiverId = "") {
  receiverId = receiverId || getIdentity()?.playerId || "";
  if (!requireMine(receiverId)) return;
  if (room.players.length < 2) {
    showToast("至少需要 2 名玩家才能收钱");
    return;
  }
  const receiver = getPlayer(receiverId);
  const payers = room.players.filter((player) => player.id !== receiver.id);
  const rentProperties = receiver?.properties.filter((prop) => !prop.mortgaged) || [];
  if (!rentProperties.length) {
    showToast("你还没有可收钱的房产");
    return;
  }
  openModal(
    "收取过路费",
    `
      <div class="form-grid">
        <div class="form-static"><span>收款玩家</span><strong>${escapeHtml(receiver.name)}</strong></div>
        <label>付款玩家
          <select name="payerId" id="payerSelect">
            ${payers.map((player) => `<option value="${player.id}">${escapeHtml(player.name)}</option>`).join("")}
          </select>
        </label>
        <label>收钱房产
          <select name="propertyId" id="propertySelect"></select>
        </label>
        <div class="form-static" id="rentPreview"><span>预计收款</span><strong>0</strong></div>
        <label>收款金额
          <input name="amount" id="rentAmount" type="number" inputmode="numeric" min="0" step="1" required />
        </label>
        ${formActions("收钱")}
      </div>
    `,
    (data) => postAction("collectRent", {
      receiverId,
      payerId: data.get("payerId"),
      propertyId: data.get("propertyId"),
      amount: data.get("amount"),
    }),
  );

  const propertySelect = qs("#propertySelect", modal);
  const rentAmount = qs("#rentAmount", modal);
  const rentPreview = qs("#rentPreview strong", modal);

  function selectedRentProperty() {
    return rentProperties.find((prop) => prop.id === propertySelect.value) || rentProperties[0];
  }

  function syncRentAmount() {
    const prop = selectedRentProperty();
    const base = Number(rentAmount.value || prop?.toll || 0);
    rentPreview.textContent = formatMoney(rentTotal(prop, base));
  }

  function syncRentForm() {
    propertySelect.innerHTML = rentProperties.map((prop) => `
      <option value="${prop.id}" data-toll="${prop.toll}">${escapeHtml(prop.name)} · ${formatMoney(rentTotal(prop))}</option>
    `).join("");
    rentAmount.value = selectedRentProperty()?.toll || 0;
    syncRentAmount();
  }
  propertySelect.addEventListener("change", () => {
    rentAmount.value = selectedRentProperty()?.toll || 0;
    syncRentAmount();
  });
  rentAmount.addEventListener("input", syncRentAmount);
  syncRentForm();
}

function showRankingModal() {
  if (!room?.started) return;
  openModal("实时排名", renderRankingList(room.players), () => true);
}

function showPropertiesModal() {
  if (!room?.started) return;
  openModal("全部房产", renderPropertyOverviewList(), () => true);
}

function showAuditModal() {
  if (!room?.started) return;
  openModal("地块盘点", renderAuditList(), () => true);
}

function showLogModal() {
  if (!room?.started) return;
  openModal("操作记录", renderLogList(room.log || []), () => true);
}

async function undoLog(logId) {
  if (pendingUndoLogId !== logId) {
    pendingUndoLogId = logId;
    clearTimeout(pendingUndoTimer);
    pendingUndoTimer = setTimeout(() => {
      pendingUndoLogId = "";
      if (modal.open && modalTitle.textContent === "操作记录") showLogModal();
    }, 3200);
    showLogModal();
    return;
  }
  pendingUndoLogId = "";
  clearTimeout(pendingUndoTimer);
  const ok = await postAction("undoLog", { logId });
  if (ok && modal.open) showLogModal();
}

async function restoreLog(logId) {
  const ok = await postAction("restoreLog", { logId });
  if (ok && modal.open) showLogModal();
}

function showQr() {
  if (!room) return;
  const link = joinLink();
  openModal(
    "扫码加入",
    `
      <div class="qr-wrap">
        <canvas id="qrCanvas" width="340" height="340"></canvas>
        <div class="join-link">${escapeHtml(link)}</div>
        <button type="button" class="secondary" data-action="copy-modal-link">复制链接</button>
      </div>
    `,
    () => true,
  );
  drawQr(link, qs("#qrCanvas", modal));
}

function joinLink() {
  const localHosts = ["localhost", "127.0.0.1", "::1"];
  const origin = localHosts.includes(location.hostname) && config?.lanOrigin ? config.lanOrigin : location.origin;
  return `${origin}/room/${room.id}`;
}

function copyJoinLink() {
  if (!room) return;
  const link = joinLink();
  if (!navigator.clipboard?.writeText) {
    showToast(link);
    return;
  }
  navigator.clipboard.writeText(link)
    .then(() => showToast("已复制加入链接"))
    .catch(() => showToast(link));
}

document.addEventListener("click", async (event) => {
  if (event.target.closest("[data-modal-close]")) {
    event.preventDefault();
    clearPendingPassStart();
    modal.close();
    return;
  }

  const target = event.target.closest("[data-action]");
  if (!target) {
    clearPendingPassStart();
    return;
  }
  const action = target.dataset.action;
  if (action !== "pass-start") clearPendingPassStart();

  if (action === "create-room") {
    await createRoom();
  } else if (action === "join-room") {
    const id = qs("#joinRoomInput")?.value.trim().toUpperCase();
    if (!id) return showToast("请输入房间号");
    location.href = `/room/${id}`;
  } else if (action === "add-setup-player") {
    const list = qs("#setupPlayers");
    if (qsa(".setup-player-row", list).length >= 8) return showToast("最多 8 名玩家");
    list.insertAdjacentHTML("beforeend", setupPlayerRow(`玩家${qsa(".setup-player-row", list).length + 1}`));
  } else if (action === "remove-setup-player") {
    const rows = qsa(".setup-player-row");
    if (rows.length <= 1) return showToast("至少保留 1 名玩家");
    target.closest(".setup-player-row").remove();
  } else if (action === "start-game") {
    const names = qsa(".setup-player-name").map((input) => input.value.trim()).filter(Boolean);
    await postAction("setup", {
      initialCash: qs("#initialCash").value,
      passStartAmount: qs("#passStartAmount").value,
      players: names,
    });
  } else if (action === "claim-player") {
    await claimPlayer(target.dataset.playerId);
  } else if (action === "switch-player") {
    if (confirm("切换后，这台手机需要重新选择玩家身份。")) {
      await releaseIdentity();
    }
  } else if (action === "copy-link" || action === "copy-modal-link") {
    copyJoinLink();
  } else if (action === "qr") {
    showQr();
  } else if (action === "show-ranking") {
    showRankingModal();
  } else if (action === "show-properties") {
    showPropertiesModal();
  } else if (action === "sort-properties") {
    propertySortMode = target.dataset.sort || "player";
    showPropertiesModal();
  } else if (action === "property-view") {
    propertyViewMode = target.dataset.view || "detail";
    showPropertiesModal();
  } else if (action === "show-audit") {
    showAuditModal();
  } else if (action === "switch-audit") {
    auditTab = target.dataset.auditTab || "count";
    showAuditModal();
  } else if (action === "show-log") {
    showLogModal();
  } else if (action === "add-property") {
    showAddProperty(target.dataset.playerId);
  } else if (action === "edit-property") {
    showEditProperty(target.dataset.playerId, target.dataset.propertyId);
  } else if (action === "color-set-property") {
    showColorSetProperty(target.dataset.playerId, target.dataset.propertyId);
  } else if (action === "upgrade-property") {
    showUpgradeProperty(target.dataset.playerId, target.dataset.propertyId);
  } else if (action === "mortgage" || action === "mortgage-property") {
    showMortgage(target.dataset.playerId, target.dataset.propertyId);
  } else if (action === "redeem-property") {
    showRedeemProperty(target.dataset.playerId, target.dataset.propertyId);
  } else if (action === "sell-building") {
    showSellBuilding(target.dataset.playerId, target.dataset.propertyId);
  } else if (action === "cash-adjust") {
    showCashAdjust(target.dataset.playerId);
  } else if (action === "pass-start") {
    await passStart(target.dataset.playerId);
  } else if (action === "edit-color") {
    showColorEditor(target.dataset.playerId);
  } else if (action === "rename-player") {
    showRenamePlayer(target.dataset.playerId);
  } else if (action === "collect-rent") {
    showCollectRent(target.dataset.playerId);
  } else if (action === "undo-log") {
    await undoLog(target.dataset.logId);
  } else if (action === "restore-log") {
    await restoreLog(target.dataset.logId);
  }
});

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function init() {
  await loadConfig();
  const id = roomIdFromPath();
  if (id) {
    await loadRoom(id);
  } else {
    render();
  }
}

init();

function drawQr(text, canvas) {
  const matrix = makeQr(text);
  const ctx = canvas.getContext("2d");
  const quiet = 4;
  const cells = matrix.length + quiet * 2;
  const size = canvas.width;
  const scale = Math.floor(size / cells);
  const offset = Math.floor((size - cells * scale) / 2);
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, size, size);
  ctx.fillStyle = "#111816";
  for (let y = 0; y < matrix.length; y += 1) {
    for (let x = 0; x < matrix.length; x += 1) {
      if (matrix[y][x]) {
        ctx.fillRect(offset + (x + quiet) * scale, offset + (y + quiet) * scale, scale, scale);
      }
    }
  }
}

function makeQr(text) {
  const version = 5;
  const size = version * 4 + 17;
  const dataCodewords = 108;
  const eccCodewords = 26;
  const bytes = [...new TextEncoder().encode(text)];
  if (bytes.length > 106) {
    throw new Error("加入链接过长，无法生成二维码");
  }

  const data = encodeQrData(bytes, dataCodewords);
  const ecc = reedSolomonRemainder(data, eccCodewords);
  const codewords = [...data, ...ecc];
  const modules = Array.from({ length: size }, () => Array(size).fill(false));
  const reserved = Array.from({ length: size }, () => Array(size).fill(false));

  const setFunction = (x, y, dark) => {
    if (x < 0 || y < 0 || x >= size || y >= size) return;
    modules[y][x] = dark;
    reserved[y][x] = true;
  };

  drawFinder(0, 0, setFunction);
  drawFinder(size - 7, 0, setFunction);
  drawFinder(0, size - 7, setFunction);
  for (let i = 8; i < size - 8; i += 1) {
    setFunction(i, 6, i % 2 === 0);
    setFunction(6, i, i % 2 === 0);
  }
  drawAlignment(30, 30, setFunction);
  setFunction(8, version * 4 + 9, true);
  reserveFormat(size, setFunction);

  const bits = [];
  for (const codeword of codewords) {
    for (let i = 7; i >= 0; i -= 1) bits.push((codeword >>> i) & 1);
  }

  let bitIndex = 0;
  let upward = true;
  for (let right = size - 1; right >= 1; right -= 2) {
    if (right === 6) right -= 1;
    for (let vert = 0; vert < size; vert += 1) {
      const y = upward ? size - 1 - vert : vert;
      for (let j = 0; j < 2; j += 1) {
        const x = right - j;
        if (reserved[y][x]) continue;
        const bit = bitIndex < bits.length ? bits[bitIndex] === 1 : false;
        modules[y][x] = bit !== mask0(x, y);
        bitIndex += 1;
      }
    }
    upward = !upward;
  }

  drawFormatBits(size, 0, setFunction);
  return modules;
}

function encodeQrData(bytes, dataCodewords) {
  const bits = [];
  appendBits(bits, 0b0100, 4);
  appendBits(bits, bytes.length, 8);
  for (const byte of bytes) appendBits(bits, byte, 8);
  const capacity = dataCodewords * 8;
  appendBits(bits, 0, Math.min(4, capacity - bits.length));
  while (bits.length % 8 !== 0) bits.push(0);
  const data = [];
  for (let i = 0; i < bits.length; i += 8) {
    data.push(bits.slice(i, i + 8).reduce((value, bit) => (value << 1) | bit, 0));
  }
  for (let pad = 0xec; data.length < dataCodewords; pad ^= 0xec ^ 0x11) {
    data.push(pad);
  }
  return data;
}

function appendBits(bits, value, length) {
  for (let i = length - 1; i >= 0; i -= 1) bits.push((value >>> i) & 1);
}

function drawFinder(left, top, setFunction) {
  for (let y = -1; y <= 7; y += 1) {
    for (let x = -1; x <= 7; x += 1) {
      const xx = left + x;
      const yy = top + y;
      const dark = x >= 0 && x <= 6 && y >= 0 && y <= 6
        && (x === 0 || x === 6 || y === 0 || y === 6 || (x >= 2 && x <= 4 && y >= 2 && y <= 4));
      setFunction(xx, yy, dark);
    }
  }
}

function drawAlignment(cx, cy, setFunction) {
  for (let y = -2; y <= 2; y += 1) {
    for (let x = -2; x <= 2; x += 1) {
      setFunction(cx + x, cy + y, Math.max(Math.abs(x), Math.abs(y)) !== 1);
    }
  }
}

function reserveFormat(size, setFunction) {
  for (let i = 0; i <= 5; i += 1) {
    setFunction(8, i, false);
    setFunction(i, 8, false);
  }
  setFunction(8, 7, false);
  setFunction(8, 8, false);
  setFunction(7, 8, false);
  for (let i = 9; i < 15; i += 1) setFunction(14 - i, 8, false);
  for (let i = 0; i < 8; i += 1) setFunction(size - 1 - i, 8, false);
  for (let i = 8; i < 15; i += 1) setFunction(8, size - 15 + i, false);
}

function drawFormatBits(size, mask, setFunction) {
  let data = (1 << 3) | mask;
  let rem = data << 10;
  for (let i = 14; i >= 10; i -= 1) {
    if (((rem >>> i) & 1) !== 0) rem ^= 0x537 << (i - 10);
  }
  const bits = ((data << 10) | (rem & 0x3ff)) ^ 0x5412;
  const bit = (i) => ((bits >>> i) & 1) !== 0;
  for (let i = 0; i <= 5; i += 1) setFunction(8, i, bit(i));
  setFunction(8, 7, bit(6));
  setFunction(8, 8, bit(7));
  setFunction(7, 8, bit(8));
  for (let i = 9; i < 15; i += 1) setFunction(14 - i, 8, bit(i));
  for (let i = 0; i < 8; i += 1) setFunction(size - 1 - i, 8, bit(i));
  for (let i = 8; i < 15; i += 1) setFunction(8, size - 15 + i, bit(i));
  setFunction(8, size - 8, true);
}

function mask0(x, y) {
  return (x + y) % 2 === 0;
}

function reedSolomonRemainder(data, degree) {
  const { exp, log } = gfTables();
  const multiply = (x, y) => (x === 0 || y === 0 ? 0 : exp[log[x] + log[y]]);
  const divisor = reedSolomonDivisor(degree, exp, multiply);
  const result = Array(degree).fill(0);
  for (const byte of data) {
    const factor = byte ^ result.shift();
    result.push(0);
    for (let i = 0; i < degree; i += 1) {
      result[i] ^= multiply(divisor[i], factor);
    }
  }
  return result;
}

function reedSolomonDivisor(degree, exp, multiply) {
  const result = Array(degree).fill(0);
  result[degree - 1] = 1;
  for (let i = 0; i < degree; i += 1) {
    const root = exp[i];
    for (let j = 0; j < degree; j += 1) {
      result[j] = multiply(result[j], root);
      if (j + 1 < degree) result[j] ^= result[j + 1];
    }
  }
  return result;
}

let cachedGf = null;

function gfTables() {
  if (cachedGf) return cachedGf;
  const exp = Array(512).fill(0);
  const log = Array(256).fill(0);
  let x = 1;
  for (let i = 0; i < 255; i += 1) {
    exp[i] = x;
    log[x] = i;
    x <<= 1;
    if (x & 0x100) x ^= 0x11d;
  }
  for (let i = 255; i < 512; i += 1) exp[i] = exp[i - 255];
  cachedGf = { exp, log };
  return cachedGf;
}
