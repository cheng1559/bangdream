const LANES = 7;

const state = {
  chart: [],
  notes: [],
  hitEvents: [],
  bpmEvents: [],
  source: "",
  pixelsPerBeat: 76,
  showBeats: true,
  currentTime: 0,
  duration: 0,
  isPlaying: false,
  playbackRate: 1,
  musicVolume: 0.8,
  animationFrame: null,
  lastFrameTime: 0,
  audio: new Audio(),
  audioReady: false,
  audioSource: "",
  hitAudioContext: null,
  playedHitEvents: new Set(),
  songRecords: [],
};

const MAX_FILTERED_SONGS = 80;

const elements = {
  canvas: document.querySelector("#chartCanvas"),
  status: document.querySelector("#status"),
  apiForm: document.querySelector("#apiForm"),
  songSearch: document.querySelector("#songSearch"),
  songSelect: document.querySelector("#songSelect"),
  songId: document.querySelector("#songId"),
  difficulty: document.querySelector("#difficulty"),
  fileInput: document.querySelector("#fileInput"),
  speedScale: document.querySelector("#speedScale"),
  playbackRate: document.querySelector("#playbackRate"),
  musicVolume: document.querySelector("#musicVolume"),
  showBeats: document.querySelector("#showBeats"),
  playPause: document.querySelector("#playPause"),
  restart: document.querySelector("#restart"),
  timeline: document.querySelector("#timeline"),
  timeReadout: document.querySelector("#timeReadout"),
  audioStatus: document.querySelector("#audioStatus"),
  objectCount: document.querySelector("#objectCount"),
  noteCount: document.querySelector("#noteCount"),
  lastBeat: document.querySelector("#lastBeat"),
  bpm: document.querySelector("#bpm"),
};

const ctx = elements.canvas.getContext("2d");
state.audio.preload = "auto";
state.audio.volume = state.musicVolume;

function setStatus(message) {
  elements.status.textContent = message;
}

function setAudioStatus(message) {
  elements.audioStatus.textContent = message;
}

function getSongTitles(song) {
  return (song.musicTitle || [])
    .filter((title) => typeof title === "string" && title.trim())
    .map((title) => title.trim());
}

function normalizeSearchText(value) {
  return String(value ?? "")
    .normalize("NFKC")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, "");
}

function fuzzyTextScore(query, target) {
  if (!query || !target) {
    return 0;
  }

  const directIndex = target.indexOf(query);
  if (directIndex >= 0) {
    return 10000 - directIndex * 10 - target.length;
  }

  let queryIndex = 0;
  let previousMatch = -1;
  let score = 0;

  for (let targetIndex = 0; targetIndex < target.length && queryIndex < query.length; targetIndex += 1) {
    if (target[targetIndex] !== query[queryIndex]) {
      continue;
    }

    score += previousMatch === targetIndex - 1 ? 18 : 8;
    previousMatch = targetIndex;
    queryIndex += 1;
  }

  return queryIndex === query.length ? score - target.length : 0;
}

function createSongRecord(id, song) {
  const titles = getSongTitles(song);
  const title = titles[0] || "未命名歌曲";
  const searchTargets = [id, ...titles]
    .map(normalizeSearchText)
    .filter(Boolean);

  return {
    id: String(id),
    numericId: Number(id),
    title,
    label: `${id}. ${title}`,
    searchTargets,
  };
}

function scoreSongRecord(record, normalizedQuery) {
  if (!normalizedQuery) {
    return 1;
  }

  return Math.max(0, ...record.searchTargets.map((target) => fuzzyTextScore(normalizedQuery, target)));
}

function filteredSongRecords(query) {
  const normalizedQuery = normalizeSearchText(query);
  if (!normalizedQuery) {
    return state.songRecords;
  }

  return state.songRecords
    .map((record) => ({ record, score: scoreSongRecord(record, normalizedQuery) }))
    .filter((item) => item.score > 0)
    .sort((a, b) => b.score - a.score || a.record.numericId - b.record.numericId)
    .slice(0, MAX_FILTERED_SONGS)
    .map((item) => item.record);
}

function renderSongOptions(query = elements.songSearch.value, selectedId = elements.songId.value) {
  const records = filteredSongRecords(query);
  if (records.length === 0) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "未找到匹配歌曲";
    elements.songSelect.replaceChildren(option);
    return;
  }

  const options = records.map((record) => {
    const option = document.createElement("option");
    option.value = record.id;
    option.textContent = record.label;
    return option;
  });

  elements.songSelect.replaceChildren(...options);

  const wantedId = String(selectedId || "");
  const hasSelectedId = records.some((record) => record.id === wantedId);
  elements.songSelect.value = hasSelectedId ? wantedId : records[0].id;

  if (elements.songSelect.value) {
    elements.songId.value = elements.songSelect.value;
  }
}

function ensureSelectedSongVisible(songId) {
  if (!state.songRecords.length) {
    return;
  }

  const wantedId = String(songId || "");
  if (!wantedId) {
    return;
  }

  const optionExists = Array.from(elements.songSelect.options).some((option) => option.value === wantedId);
  if (!optionExists) {
    elements.songSearch.value = "";
    renderSongOptions("", wantedId);
  }

  elements.songSelect.value = wantedId;
}

function getItemBeat(item) {
  if (typeof item.beat === "number") {
    return item.beat;
  }
  if (Array.isArray(item.connections) && item.connections.length > 0) {
    return item.connections[0].beat ?? 0;
  }
  return 0;
}

function getLastBeat(chart) {
  let last = 0;
  for (const item of chart) {
    if (Array.isArray(item.connections)) {
      for (const connection of item.connections) {
        last = Math.max(last, connection.beat ?? 0);
      }
    } else {
      last = Math.max(last, item.beat ?? 0);
    }
  }
  return last;
}

function buildBpmEvents(chart) {
  const events = chart
    .filter((item) => item.type === "BPM" && typeof item.bpm === "number")
    .map((item) => ({ beat: item.beat ?? 0, bpm: item.bpm }))
    .sort((a, b) => a.beat - b.beat);

  if (events.length === 0 || events[0].beat > 0) {
    events.unshift({ beat: 0, bpm: events[0]?.bpm ?? 120 });
  }

  let elapsed = 0;
  for (let index = 0; index < events.length; index += 1) {
    const event = events[index];
    event.time = elapsed;
    const next = events[index + 1];
    if (next) {
      elapsed += ((next.beat - event.beat) * 60) / event.bpm;
    }
  }

  return events;
}

function beatToSeconds(beat) {
  const events = state.bpmEvents;
  let current = events[0] ?? { beat: 0, bpm: 120, time: 0 };
  for (const event of events) {
    if (event.beat <= beat) {
      current = event;
    } else {
      break;
    }
  }
  return current.time + ((beat - current.beat) * 60) / current.bpm;
}

function secondsToBeat(seconds) {
  const events = state.bpmEvents;
  let current = events[0] ?? { beat: 0, bpm: 120, time: 0 };
  for (const event of events) {
    if (event.time <= seconds) {
      current = event;
    } else {
      break;
    }
  }
  return current.beat + ((seconds - current.time) * current.bpm) / 60;
}

function normalizeNotes(chart) {
  const notes = [];

  for (const item of chart) {
    if (item.type === "Single") {
      notes.push({
        type: "Single",
        lane: item.lane,
        beat: item.beat,
        flick: Boolean(item.flick),
      });
    }

    if (item.type === "Directional") {
      notes.push({
        type: "Directional",
        lane: item.lane,
        beat: item.beat,
        direction: item.direction,
        width: item.width ?? 1,
        flick: true,
      });
    }

    if ((item.type === "Long" || item.type === "Slide") && Array.isArray(item.connections)) {
      notes.push({
        type: item.type,
        connections: item.connections.map((point) => ({
          lane: point.lane,
          beat: point.beat,
          flick: Boolean(point.flick),
        })),
      });
    }
  }

  return notes.sort((a, b) => getItemBeat(a) - getItemBeat(b));
}

function buildHitEvents(notes) {
  const events = [];

  notes.forEach((note, noteIndex) => {
    if (note.type === "Single") {
      events.push({
        id: `single:${noteIndex}:${note.lane}:${note.beat}`,
        beat: note.beat,
        flick: note.flick,
      });
    }

    if (note.type === "Directional") {
      events.push({
        id: `directional:${noteIndex}:${note.lane}:${note.beat}:${note.direction}`,
        beat: note.beat,
        flick: true,
      });
    }

    if ((note.type === "Long" || note.type === "Slide") && Array.isArray(note.connections)) {
      note.connections.forEach((point, index) => {
        events.push({
          id: `${note.type}:${noteIndex}:${index}:${point.lane}:${point.beat}`,
          beat: point.beat,
          flick: point.flick,
        });
      });
    }
  });

  return events
    .map((event) => ({ ...event, time: beatToSeconds(event.beat) }))
    .sort((a, b) => a.time - b.time);
}

function getStats(chart) {
  const bpm = [];
  let noteCount = 0;

  for (const item of chart) {
    if (item.type === "BPM" && typeof item.bpm === "number") {
      bpm.push(item.bpm);
    }
    if (item.type !== "BPM" && item.type !== "System") {
      noteCount += 1;
    }
  }

  return {
    objectCount: chart.length,
    noteCount,
    lastBeat: getLastBeat(chart),
    bpm: [...new Set(bpm)],
  };
}

function updateSummary() {
  const stats = getStats(state.chart);
  elements.objectCount.textContent = stats.objectCount || "-";
  elements.noteCount.textContent = stats.noteCount || "-";
  elements.lastBeat.textContent = stats.lastBeat ? stats.lastBeat.toFixed(2) : "-";
  elements.bpm.textContent = stats.bpm.length ? stats.bpm.join(", ") : "-";
}

function formatTime(seconds) {
  const safe = Math.max(0, seconds);
  const minutes = Math.floor(safe / 60);
  const rest = safe - minutes * 60;
  return `${String(minutes).padStart(2, "0")}:${rest.toFixed(2).padStart(5, "0")}`;
}

function updateTransport() {
  elements.timeline.max = String(Math.max(0, state.duration));
  elements.timeline.value = String(Math.min(state.currentTime, state.duration));
  elements.timeReadout.textContent = `${formatTime(state.currentTime)} / ${formatTime(state.duration)}`;
  elements.playPause.textContent = state.isPlaying ? "暂停" : "播放";
}

function ensureHitAudioContext() {
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextClass) {
    return null;
  }
  if (!state.hitAudioContext) {
    state.hitAudioContext = new AudioContextClass();
  }
  if (state.hitAudioContext.state === "suspended") {
    state.hitAudioContext.resume();
  }
  return state.hitAudioContext;
}

function playHitSound(flick = false) {
  const audioContext = ensureHitAudioContext();
  if (!audioContext) {
    return;
  }

  const now = audioContext.currentTime;
  const oscillator = audioContext.createOscillator();
  const gain = audioContext.createGain();

  oscillator.type = flick ? "triangle" : "sine";
  oscillator.frequency.setValueAtTime(flick ? 1320 : 880, now);
  oscillator.frequency.exponentialRampToValueAtTime(flick ? 1760 : 660, now + 0.035);
  gain.gain.setValueAtTime(0.0001, now);
  gain.gain.exponentialRampToValueAtTime(flick ? 0.18 : 0.14, now + 0.004);
  gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.055);

  oscillator.connect(gain);
  gain.connect(audioContext.destination);
  oscillator.start(now);
  oscillator.stop(now + 0.06);
}

function playDueHitSounds(previousTime, currentTime) {
  if (!state.isPlaying || currentTime < previousTime) {
    return;
  }

  const lookBehind = 0.018;
  for (const event of state.hitEvents) {
    if (event.time + lookBehind < previousTime) {
      continue;
    }
    if (event.time > currentTime + lookBehind) {
      break;
    }
    if (!state.playedHitEvents.has(event.id)) {
      state.playedHitEvents.add(event.id);
      playHitSound(event.flick);
    }
  }
}

function paddedSongId(songId) {
  return String(songId).padStart(3, "0");
}

async function loadAudioForSong(songId) {
  state.audio.pause();
  state.audio.removeAttribute("src");
  state.audioReady = false;
  state.audioSource = "";
  setAudioStatus("音乐加载中");

  try {
    const url = `/bestdori/song-audio/${songId}.mp3`;
    state.audio.src = url;
    state.audio.playbackRate = state.playbackRate;
    state.audio.volume = state.musicVolume;
    state.audio.currentTime = Math.min(state.currentTime, state.duration);
    state.audioSource = url;
    state.audioReady = true;
    setAudioStatus(`音乐: bgm${paddedSongId(songId)}`);
  } catch (error) {
    setAudioStatus(`音乐加载失败: ${error.message}`);
  }
}

function inferSongIdFromChart(chart) {
  const system = chart.find((item) => item.type === "System" && typeof item.data === "string");
  const match = system?.data?.match(/bgm(\d+)\.wav/i);
  if (match) {
    return Number(match[1]);
  }
  return Number(elements.songId.value) || 1;
}

function canvasSize() {
  const rect = elements.canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  elements.canvas.width = Math.max(1, Math.floor(rect.width * ratio));
  elements.canvas.height = Math.max(1, Math.floor(rect.height * ratio));
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  return { width: rect.width, height: rect.height };
}

function metricsFor(size) {
  const left = size.width < 640 ? 42 : 64;
  const rightPad = size.width < 640 ? 16 : 28;
  const top = 24;
  const bottom = 50;
  const right = size.width - rightPad;
  const laneWidth = Math.max(38, (right - left) / LANES);
  const judgeY = size.height - bottom;

  return {
    left,
    top,
    right,
    bottom,
    laneWidth,
    judgeY,
    height: size.height,
    width: size.width,
  };
}

function laneX(lane, metrics) {
  return metrics.left + lane * metrics.laneWidth + metrics.laneWidth / 2;
}

function beatY(beat, currentBeat, metrics) {
  return metrics.judgeY - (beat - currentBeat) * state.pixelsPerBeat;
}

function drawRoundedRect(x, y, width, height, radius) {
  const r = Math.min(radius, width / 2, height / 2);
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + width - r, y);
  ctx.quadraticCurveTo(x + width, y, x + width, y + r);
  ctx.lineTo(x + width, y + height - r);
  ctx.quadraticCurveTo(x + width, y + height, x + width - r, y + height);
  ctx.lineTo(x + r, y + height);
  ctx.quadraticCurveTo(x, y + height, x, y + height - r);
  ctx.lineTo(x, y + r);
  ctx.quadraticCurveTo(x, y, x + r, y);
  ctx.closePath();
}

function drawFlick(x, y) {
  ctx.save();
  ctx.strokeStyle = "#c2255c";
  ctx.lineWidth = 3;
  ctx.lineCap = "round";
  ctx.beginPath();
  ctx.moveTo(x - 12, y + 10);
  ctx.lineTo(x, y - 10);
  ctx.lineTo(x + 12, y + 10);
  ctx.stroke();
  ctx.restore();
}

function drawDirectional(note, currentBeat, metrics) {
  const x = laneX(note.lane, metrics);
  const y = beatY(note.beat, currentBeat, metrics);
  if (y < -44 || y > metrics.height + 44) {
    return;
  }

  const sign = note.direction === "Left" ? -1 : 1;
  const width = Math.max(1, Number(note.width) || 1);
  const bodyLength = Math.min(metrics.laneWidth * width * 0.82, metrics.laneWidth * 3.2);
  const startX = x - sign * bodyLength * 0.28;
  const endX = x + sign * bodyLength * 0.72;
  const height = 14;
  const head = Math.min(22, Math.max(14, metrics.laneWidth * 0.24));

  ctx.save();
  ctx.fillStyle = "#c2255c";
  ctx.strokeStyle = "rgba(255,255,255,0.94)";
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.beginPath();
  ctx.moveTo(endX, y);
  ctx.lineTo(endX - sign * head, y - height);
  ctx.lineTo(endX - sign * head, y - height * 0.48);
  ctx.lineTo(startX, y - height * 0.48);
  ctx.lineTo(startX, y + height * 0.48);
  ctx.lineTo(endX - sign * head, y + height * 0.48);
  ctx.lineTo(endX - sign * head, y + height);
  ctx.closePath();
  ctx.fill();
  ctx.stroke();
  ctx.restore();
}

function drawNote(lane, beat, currentBeat, metrics, color, flick = false) {
  const x = laneX(lane, metrics);
  const y = beatY(beat, currentBeat, metrics);
  if (y < -36 || y > metrics.height + 36) {
    return;
  }

  const width = Math.max(32, metrics.laneWidth * 0.74);
  const height = 12;

  ctx.save();
  ctx.fillStyle = color;
  ctx.strokeStyle = "rgba(255,255,255,0.9)";
  ctx.lineWidth = 2;
  drawRoundedRect(x - width / 2, y - height / 2, width, height, 6);
  ctx.fill();
  ctx.stroke();
  ctx.restore();

  if (flick) {
    drawFlick(x, y - 16);
  }
}

function drawConnectionPath(note, currentBeat, metrics, color) {
  const points = note.connections || [];
  if (points.length < 2) {
    return;
  }

  const visible = points.some((point) => {
    const y = beatY(point.beat, currentBeat, metrics);
    return y >= -80 && y <= metrics.height + 80;
  });
  if (!visible) {
    return;
  }

  ctx.save();
  ctx.strokeStyle = color;
  ctx.lineWidth = Math.max(9, metrics.laneWidth * 0.24);
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.globalAlpha = 0.72;
  ctx.beginPath();
  points.forEach((point, index) => {
    const x = laneX(point.lane, metrics);
    const y = beatY(point.beat, currentBeat, metrics);
    if (index === 0) {
      ctx.moveTo(x, y);
    } else {
      ctx.lineTo(x, y);
    }
  });
  ctx.stroke();
  ctx.restore();

  for (const point of points) {
    drawNote(point.lane, point.beat, currentBeat, metrics, color, point.flick);
  }
}

function drawGrid(currentBeat, metrics) {
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, metrics.width, metrics.height);

  const laneHeight = metrics.judgeY - metrics.top;
  for (let lane = 0; lane < LANES; lane += 1) {
    ctx.fillStyle = lane % 2 === 0 ? "#f8fafc" : "#eef2f8";
    ctx.fillRect(metrics.left + lane * metrics.laneWidth, metrics.top, metrics.laneWidth, laneHeight);
  }

  ctx.strokeStyle = "#dfe4ef";
  ctx.lineWidth = 1;
  for (let lane = 0; lane <= LANES; lane += 1) {
    const x = metrics.left + lane * metrics.laneWidth;
    ctx.beginPath();
    ctx.moveTo(x, metrics.top);
    ctx.lineTo(x, metrics.judgeY);
    ctx.stroke();
  }

  if (state.showBeats) {
    const firstBeat = Math.floor(currentBeat - 2);
    const lastBeat = Math.ceil(currentBeat + (metrics.judgeY - metrics.top) / state.pixelsPerBeat + 1);
    for (let beat = firstBeat; beat <= lastBeat; beat += 1) {
      if (beat < 0) {
        continue;
      }
      const y = beatY(beat, currentBeat, metrics);
      if (y < metrics.top || y > metrics.judgeY + 20) {
        continue;
      }
      const isMeasure = beat % 4 === 0;
      ctx.strokeStyle = isMeasure ? "#9aa7bb" : "#e7ebf2";
      ctx.lineWidth = isMeasure ? 1.3 : 1;
      ctx.beginPath();
      ctx.moveTo(metrics.left, y);
      ctx.lineTo(metrics.right, y);
      ctx.stroke();

      if (isMeasure) {
        ctx.fillStyle = "#657086";
        ctx.font = "12px Segoe UI, sans-serif";
        ctx.textAlign = "right";
        ctx.textBaseline = "middle";
        ctx.fillText(String(beat), metrics.left - 10, y);
      }
    }
  }

  ctx.save();
  ctx.strokeStyle = "#202b3d";
  ctx.lineWidth = 3;
  ctx.beginPath();
  ctx.moveTo(metrics.left, metrics.judgeY);
  ctx.lineTo(metrics.right, metrics.judgeY);
  ctx.stroke();
  ctx.fillStyle = "#202b3d";
  ctx.font = "12px Segoe UI, sans-serif";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  ctx.fillText("判定线", metrics.left - 10, metrics.judgeY);
  ctx.restore();

  ctx.fillStyle = "#172033";
  ctx.font = "12px Segoe UI, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  for (let lane = 0; lane < LANES; lane += 1) {
    ctx.fillText(String(lane), laneX(lane, metrics), metrics.height - 22);
  }
}

function drawChart() {
  const size = canvasSize();
  const metrics = metricsFor(size);
  const currentBeat = secondsToBeat(state.currentTime);

  drawGrid(currentBeat, metrics);

  for (const note of state.notes) {
    if (note.type === "Long") {
      drawConnectionPath(note, currentBeat, metrics, "#2f9e44");
    } else if (note.type === "Slide") {
      drawConnectionPath(note, currentBeat, metrics, "#d9480f");
    }
  }

  for (const note of state.notes) {
    if (note.type === "Single") {
      drawNote(note.lane, note.beat, currentBeat, metrics, "#168aad", note.flick);
    } else if (note.type === "Directional") {
      drawDirectional(note, currentBeat, metrics);
    }
  }

  updateTransport();
}

function stopPlayback() {
  state.isPlaying = false;
  state.lastFrameTime = 0;
  state.audio.pause();
  if (state.animationFrame) {
    cancelAnimationFrame(state.animationFrame);
    state.animationFrame = null;
  }
  updateTransport();
}

function animationTick(timestamp) {
  if (!state.isPlaying) {
    return;
  }

  if (!state.lastFrameTime) {
    state.lastFrameTime = timestamp;
  }

  const deltaSeconds = (timestamp - state.lastFrameTime) / 1000;
  state.lastFrameTime = timestamp;
  const previousTime = state.currentTime;
  if (state.audioReady && !state.audio.paused) {
    state.currentTime = Math.min(state.duration, state.audio.currentTime);
  } else {
    state.currentTime = Math.min(state.duration, state.currentTime + deltaSeconds * state.playbackRate);
  }
  playDueHitSounds(previousTime, state.currentTime);

  if (state.currentTime >= state.duration) {
    stopPlayback();
    drawChart();
    return;
  }

  drawChart();
  state.animationFrame = requestAnimationFrame(animationTick);
}

function startPlayback() {
  if (!state.chart.length) {
    return;
  }
  if (state.currentTime >= state.duration) {
    state.currentTime = 0;
  }
  state.isPlaying = true;
  state.lastFrameTime = 0;
  ensureHitAudioContext();
  updateTransport();

  if (state.audioReady) {
    state.audio.currentTime = Math.min(state.currentTime, state.audio.duration || state.duration);
    state.audio.playbackRate = state.playbackRate;
    state.audio.play().catch((error) => {
      setAudioStatus(`音乐播放失败: ${error.message}`);
    });
  }

  state.animationFrame = requestAnimationFrame(animationTick);
}

async function loadChart(chart, source, options = {}) {
  if (!Array.isArray(chart)) {
    throw new Error("谱面 JSON 顶层必须是数组");
  }

  stopPlayback();
  state.chart = chart;
  state.notes = normalizeNotes(chart);
  state.bpmEvents = buildBpmEvents(chart);
  state.hitEvents = buildHitEvents(state.notes);
  state.playedHitEvents.clear();
  state.source = source;
  state.currentTime = 0;
  state.duration = beatToSeconds(getLastBeat(chart));

  updateSummary();
  drawChart();
  setStatus(source);

  const songId = options.songId ?? inferSongIdFromChart(chart);
  await loadAudioForSong(songId);
}

async function loadFromLocalCharts(songId, difficulty) {
  const localUrl = `/charts/${difficulty}/${songId}.json`;
  setStatus(`加载中: ${localUrl}`);
  const localResponse = await fetch(localUrl);
  if (!localResponse.ok) {
    throw new Error(`charts 返回 ${localResponse.status}`);
  }
  const localChart = await localResponse.json();
  await loadChart(localChart, `已加载 charts: ${difficulty}/${songId}.json`, { songId: Number(songId) });
}

async function loadSongList() {
  try {
    const response = await fetch("/charts/all.1.json");
    if (!response.ok) {
      throw new Error(`charts 返回 ${response.status}`);
    }

    const songs = await response.json();
    state.songRecords = Object.entries(songs)
      .filter(([, song]) => song && Array.isArray(song.musicTitle))
      .sort((a, b) => Number(a[0]) - Number(b[0]))
      .map(([id, song]) => createSongRecord(id, song));

    renderSongOptions();
  } catch (error) {
    state.songRecords = [];
    const option = document.createElement("option");
    option.value = "";
    option.textContent = `歌曲列表加载失败: ${error.message}`;
    elements.songSelect.replaceChildren(option);
  }
}

async function loadDefaultLocal() {
  try {
    const response = await fetch(`/charts/${elements.difficulty.value}/${elements.songId.value}.json`);
    if (!response.ok) {
      drawChart();
      return;
    }
    const chart = await response.json();
    await loadChart(chart, `已加载 charts: ${elements.difficulty.value}/${elements.songId.value}.json`);
  } catch (error) {
    setStatus("可以输入 ID/难度加载 charts，或打开 JSON 文件");
    drawChart();
  }
}

elements.apiForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await loadFromLocalCharts(elements.songId.value, elements.difficulty.value);
  } catch (error) {
    setStatus(`加载失败: ${error.message}`);
  }
});

elements.songSelect.addEventListener("change", () => {
  if (elements.songSelect.value) {
    elements.songId.value = elements.songSelect.value;
  }
});

elements.songSearch.addEventListener("input", () => {
  renderSongOptions(elements.songSearch.value);
});

elements.songId.addEventListener("input", () => {
  ensureSelectedSongVisible(elements.songId.value);
});

elements.fileInput.addEventListener("change", async () => {
  const file = elements.fileInput.files?.[0];
  if (!file) {
    return;
  }

  try {
    const text = await file.text();
    await loadChart(JSON.parse(text), `已加载文件: ${file.name}`);
  } catch (error) {
    setStatus(`文件读取失败: ${error.message}`);
  } finally {
    elements.fileInput.value = "";
  }
});

elements.speedScale.addEventListener("input", () => {
  state.pixelsPerBeat = Number(elements.speedScale.value);
  drawChart();
});

elements.playbackRate.addEventListener("change", () => {
  state.playbackRate = Number(elements.playbackRate.value);
  state.audio.playbackRate = state.playbackRate;
  updateTransport();
});

elements.musicVolume.addEventListener("input", () => {
  state.musicVolume = Number(elements.musicVolume.value) / 100;
  state.audio.volume = state.musicVolume;
});

elements.showBeats.addEventListener("change", () => {
  state.showBeats = elements.showBeats.checked;
  drawChart();
});

elements.playPause.addEventListener("click", () => {
  if (state.isPlaying) {
    stopPlayback();
  } else {
    ensureHitAudioContext();
    startPlayback();
  }
});

elements.restart.addEventListener("click", () => {
  stopPlayback();
  state.currentTime = 0;
  state.playedHitEvents.clear();
  state.audio.currentTime = 0;
  drawChart();
});

elements.timeline.addEventListener("input", () => {
  state.currentTime = Number(elements.timeline.value);
  state.playedHitEvents.clear();
  if (state.audioReady) {
    state.audio.currentTime = Math.min(state.currentTime, state.audio.duration || state.duration);
  }
  drawChart();
});

state.audio.addEventListener("loadedmetadata", () => {
  if (Number.isFinite(state.audio.duration) && state.audio.duration > 0) {
    state.duration = Math.max(state.duration, state.audio.duration);
    updateTransport();
  }
});

state.audio.addEventListener("ended", () => {
  stopPlayback();
  state.currentTime = state.duration;
  drawChart();
});

state.audio.addEventListener("error", () => {
  state.audioReady = false;
  setAudioStatus("音乐加载失败");
});

window.addEventListener("resize", drawChart);

loadSongList();
loadDefaultLocal();
