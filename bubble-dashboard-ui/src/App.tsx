import { useState, useMemo, useEffect, useCallback, useRef, memo, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { motion, AnimatePresence } from 'framer-motion';
import { Activity, Droplets, Gauge, Beaker, FileDown, Layers, Target, AlertTriangle, AlertCircle, CheckCircle2, BarChart3, ScanEye, Video, Pause, Play, RotateCcw, Square, WifiOff, type LucideIcon } from 'lucide-react';
import ReactECharts from 'echarts-for-react';
import jsPDF from 'jspdf';
import html2canvas from 'html2canvas';
import LiveSparkChart from './LiveSparkChart';


// ─── WebSocket hook: заменяет polling /api/live_stats каждые 500мс ───────────
function useLiveStatsWs(): LiveStats | null {
  const [stats, setStats] = useState<LiveStats | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  useEffect(() => {
    const connect = () => {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      const ws = new WebSocket(`${proto}://${location.host}/api/ws/stats`);
      wsRef.current = ws;
      ws.onmessage = e => { try { setStats(JSON.parse(e.data)); } catch { /* битое сообщение — ждём следующее */ } };
      ws.onclose   = () => { setTimeout(connect, 2000); };  // авто-реконнект
      ws.onerror   = () => ws.close();
    };
    connect();
    return () => wsRef.current?.close();
  }, []);
  return stats;
}

const LiveMiniChart = memo(({ stats }: { stats: LiveStats | null }) => {
  if (!stats?.active) return null;
  return (
    <div className="flex flex-col gap-1 py-1">
      {[
        { label: 'На экране', val: stats.on_screen,            color: 'text-primary' },
        { label: 'Поток /с',  val: stats.rate.toFixed(2),      color: 'text-secondary' },
        { label: 'Ср. радиус',val: `${stats.avg_radius} px`,   color: 'text-accent' },
        { label: 'Кадр #',   val: stats.frame_n ?? '—',        color: 'text-slate-500' },
      ].map(({ label, val, color }) => (
        <div key={label} className="flex justify-between items-center py-1 border-b border-slate-100 last:border-0">
          <span className="text-xs text-slate-500 font-sans">{label}</span>
          <span className={`text-sm font-bold font-mono ${color}`}>{val}</span>
        </div>
      ))}
    </div>
  );
});

// Используем относительный путь '/api' вместо 'http://localhost:8000/api'
// В Docker: Nginx проксирует /api/ → api:8000 (см. Dockerfile)
// При разработке (npm run dev): Vite проксирует /api → localhost:8000 (см. vite.config.ts)
const API_URL = '/api';
// API принимает команды только с этим заголовком (защита от запросов из чужих вкладок)
const DASHBOARD_HEADERS = { 'X-Bubble-Client': 'dashboard' };

// --- Interfaces ---
interface Session {
  start_time: string;
  source: string;
  duration_sec: number;
  total_bubbles: number;
  avg_radius_px: number;
  rate_per_sec: number;
  roi: string;
  notes: string;
  res_w?: number;
  res_h?: number;
  fps_actual?: number;
  px_per_mm?: number;
  det_hough?: number;
  det_contour?: number;
  det_blob?: number;
  use_convex?: boolean;
  use_watershed?: boolean;
}

interface BubbleDetail {
  ID: number;
  Frame: number;
  Time_ms?: number;
  Radius_px: number;
  Radius_mm: number;
  a_min_mm?: number;
  a_max_mm?: number;
  Eccentricity?: number;
  Angle_deg?: number;
  Speed_px_s: number;
  Speed_mm_s: number;
  X_px?: number;
  Speed_X_px?: number;
  Age_frames?: number;
  Wobble_px?: number;
  Mineralization_Index?: number;
  Reynolds_Number?: number;
  V_Stokes_px?: number;
  Delta_V_ms?: number;
}

interface LiveStats {
  active: boolean;
  total: number;
  on_screen: number;
  rate: number;
  elapsed: number;
  avg_radius: number;
  source?: string;
  paused?: boolean;
  frame_n?: number;
  speed_hist?:  number[];   // история скорости для React-графика
  radius_hist?: number[];   // история радиуса для React-графика
}

// --- Fetchers ---
const fetchSessions = async (): Promise<Session[]> => {
  const res = await fetch(`${API_URL}/sessions`);
  if (!res.ok) throw new Error('Failed to fetch sessions');
  return res.json();
};

const fetchDetails = async (sessionId: string): Promise<BubbleDetail[]> => {
  const safeId = sessionId.replace(/:/g, '-').replace(/ /g, '_');
  const res = await fetch(`${API_URL}/details/${safeId}`);
  if (!res.ok) {
    if (res.status === 404) return [];
    throw new Error('Details fetch error');
  }
  return res.json();
};



// Отправка команды трекеру
const sendTrackerCommand = async (cmd: string): Promise<void> => {
  await fetch(`${API_URL}/tracker/command?cmd=${encodeURIComponent(cmd)}`, { method: 'POST', headers: DASHBOARD_HEADERS });
};


// --- Helper Functions ---
const parseNumber = (val: unknown): number => {
  if (typeof val === 'number') return val;
  if (typeof val === 'string') return parseFloat(val.replace(',', '.')) || 0;
  return 0;
};

// --- KPI Card ---
interface MetricCardProps {
  title: string;
  value: ReactNode;
  icon: LucideIcon;
  delay?: number;
  subtext?: string;
  tooltip?: ReactNode;
  unit?: string;
}

const MetricCard = ({ title, value, icon: Icon, delay, subtext, tooltip, unit }: MetricCardProps) => {
  const [showTip, setShowTip] = useState(false);
  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.5, delay }}
      className="relative overflow-hidden card card-hoverable flex flex-col justify-between"
      onMouseEnter={() => setShowTip(true)} onMouseLeave={() => setShowTip(false)}
    >
      <div className="flex justify-between items-start z-10 relative">
        <div className="flex-1 pr-2">
          <p className="text-slate-500 text-xs font-semibold uppercase tracking-wider mb-2 font-sans">{title}</p>
          <div className="flex items-baseline gap-1">
            <h3 className="text-3xl font-bold text-foreground tracking-tight font-mono">{value}</h3>
            {unit && <span className="text-slate-500 text-sm font-medium font-mono">{unit}</span>}
          </div>
          {subtext && <p className="text-[12px] text-slate-600 mt-2 leading-relaxed font-sans">{subtext}</p>}
        </div>
        <div className="p-3 rounded-xl bg-blue-50 border border-blue-100 text-primary">
          <Icon className="w-5 h-5" />
        </div>
      </div>
      <AnimatePresence>
        {tooltip && showTip && (
          <motion.div initial={{ opacity: 0, y: 5 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 5 }}
            className="absolute bottom-full left-0 mb-2 bg-foreground border border-blue-800 rounded-xl p-3 text-xs text-white shadow-xl z-50 w-64 pointer-events-none">
            {tooltip}
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
};

// ─────────────────────────────────────────────────────────────────────────────
// 🎥 LIVE VIDEO PANEL — MJPEG стрим из bubble_tracker_pro.py
// ─────────────────────────────────────────────────────────────────────────────
const LiveVideoPanel = () => {
  const [lineY, setLineY]           = useState(50);
  const [cmdSending, setCmdSending] = useState<string | null>(null);

  // WebSocket вместо polling — рендер видео и рендер стата независимы
  const liveStats = useLiveStatsWs();
  const streamOk  = !!liveStats?.active;

  // Отправляем команду + блокируем кнопку на 300мс (anti-spam)
  const cmd = useCallback(async (c: string) => {
    setCmdSending(c);
    await sendTrackerCommand(c);
    setTimeout(() => setCmdSending(null), 300);
  }, []);


  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.6, delay: 0.3 }}
      className="relative mb-6 rounded-2xl overflow-hidden card p-0"
    >
      {/* Заголовок панели */}
      <div className="relative z-10 flex items-center justify-between px-6 py-4 border-b border-border bg-white">
        <div className="flex items-center gap-3">
          <div className="p-2 rounded-lg bg-blue-50 border border-blue-100">
            <Video className="w-4 h-4 text-primary" />
          </div>
          <div>
            <span className="text-foreground font-bold text-sm tracking-wide">LIVE ВИДЕОПОТОК</span>
            {liveStats?.source && (
              <span className="ml-2 text-xs text-slate-500 font-mono">{liveStats.source}</span>
            )}
          </div>
          {/* Индикатор статуса стрима */}
          <div className="flex items-center gap-1.5 ml-4">
            {streamOk && liveStats?.active ? (
              <><span className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse shadow-[0_0_8px_rgba(16,185,129,0.5)]" />
              <span className="text-xs text-emerald-600 font-medium tracking-wide">LIVE</span></>
            ) : (
              <><WifiOff className="w-3 h-3 text-slate-400" />
              <span className="text-xs text-slate-500">{streamOk ? 'ОЖИДАНИЕ' : 'НЕТ СИГНАЛА'}</span></>
            )}
          </div>
        </div>

        {/* Кнопки управления */}
        <div className="flex items-center gap-2">
          {/* Пауза / Продолжить */}
          <button
            onClick={() => cmd(liveStats?.paused ? 'resume' : 'pause')}
            disabled={!streamOk || !liveStats?.active || cmdSending === 'pause' || cmdSending === 'resume'}
            title={liveStats?.paused ? 'Продолжить (SPACE)' : 'Пауза (SPACE)'}
            className="p-2 rounded-lg bg-slate-50 hover:bg-blue-50 border border-slate-200 hover:border-primary transition-all disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer"
          >
            {liveStats?.paused
              ? <Play className="w-4 h-4 text-emerald-600" />
              : <Pause className="w-4 h-4 text-primary" />}
          </button>
          {/* Сброс счётчика */}
          <button
            onClick={() => cmd('reset')}
            disabled={!streamOk || !liveStats?.active || cmdSending === 'reset'}
            title="Сбросить счётчик (R)"
            className="p-2 rounded-lg bg-slate-50 hover:bg-amber-50 border border-slate-200 hover:border-accent transition-all disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer"
          >
            <RotateCcw className="w-4 h-4 text-accent" />
          </button>
          {/* Стоп */}
          <button
            onClick={() => cmd('stop')}
            disabled={!streamOk || !liveStats?.active || cmdSending === 'stop'}
            title="Завершить сессию (ESC)"
            className="p-2 rounded-lg bg-slate-50 hover:bg-red-50 border border-slate-200 hover:border-red-500 transition-all disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer"
          >
            <Square className="w-4 h-4 text-red-600" />
          </button>
        </div>
      </div>

      {/* Основная область: видео + live-stats */}
      <div className="relative z-10 flex flex-col lg:flex-row gap-0 bg-slate-50">

        {/* Видео */}
        <div className="video-stream-container flex-1 relative min-h-[300px] lg:min-h-[400px] flex items-center justify-center bg-black/5">
          {streamOk ? (
            <img
              src="/api/stream"
              alt="Live bubble tracking"
              className="w-full h-full object-contain mix-blend-multiply"
              style={{ maxHeight: '480px', display: 'block' }}
            />
          ) : (
            <div className="flex flex-col items-center gap-4 text-center p-8">
              <div className="p-4 rounded-full bg-slate-200">
                <Video className="w-10 h-10 text-slate-400" />
              </div>
              <div>
                <p className="text-slate-600 font-medium">Видеопоток не активен</p>
                <p className="text-slate-500 text-sm mt-1">Запустите <code className="text-primary font-mono bg-slate-100 px-1 rounded">bubble_tracker_pro.py</code></p>
              </div>
            </div>
          )}
          {liveStats?.paused && (
            <div className="absolute inset-0 flex items-center justify-center bg-white/60 backdrop-blur-sm">
              <div className="flex items-center gap-3 px-6 py-3 rounded-2xl border border-blue-200 bg-white shadow-lg">
                <Pause className="w-6 h-6 text-primary" />
                <span className="text-foreground font-bold tracking-widest">ПАУЗА</span>
              </div>
            </div>
          )}
        </div>

        {/* Боковая панель */}
        {liveStats?.active && (
          <div className="w-full lg:w-64 flex flex-col gap-4 p-5 border-t lg:border-t-0 lg:border-l border-border bg-white">
            <p className="text-[10px] text-slate-400 uppercase font-bold tracking-widest font-sans">Live Метрики</p>
            <div className="rounded-xl p-4 text-center border border-blue-100 bg-blue-50/50 shadow-sm">
              <p className="text-4xl font-black text-primary font-mono tracking-tight">{liveStats.total}</p>
              <p className="text-xs text-slate-500 mt-1 font-medium uppercase tracking-wide">пузырьков</p>
            </div>
            
            <LiveMiniChart stats={liveStats} />

            {(liveStats.speed_hist?.length ?? 0) >= 2 && (
              <LiveSparkChart
                speedHist={liveStats.speed_hist!}
                radiusHist={liveStats.radius_hist ?? []}
              />
            )}

            <div className="mt-auto pt-4 border-t border-slate-100">
              <p className="text-[10px] text-slate-400 uppercase font-bold tracking-widest mb-2 font-sans">Счётная линия</p>
              <input type="range" min={5} max={95} value={lineY}
                onChange={e => setLineY(Number(e.target.value))}
                className="w-full accent-primary cursor-pointer" style={{ direction: 'rtl' }} />
              <div className="flex justify-between text-[10px] text-slate-500 mt-1 font-medium">
                <span>Верх</span><span className="text-primary font-mono font-bold">{lineY}%</span><span>Низ</span>
              </div>
              <button onClick={() => void fetch(`${API_URL}/tracker/line_y?pct=${lineY}`, { method: 'POST', headers: DASHBOARD_HEADERS })}
                className="w-full mt-2 py-2 text-xs font-semibold bg-blue-50 hover:bg-blue-100 border border-blue-200 rounded-lg text-primary transition-colors cursor-pointer">
                Применить
              </button>
            </div>
          </div>
        )}
      </div>
    </motion.div>
  );
};

export default function App() {
  const [sessionAId, setSessionAId] = useState<string | null>(null);
  const [sessionBId, setSessionBId] = useState<string | null>(null);
  const [isExporting, setIsExporting] = useState(false);
  // Фильтр: скрывать нулевые сессии (duration=0 или total_bubbles=0)
  const [hideEmpty, setHideEmpty] = useState(true);

  const { data: sessions, isLoading: loadingSessions, isError: sessionsError } = useQuery({
    queryKey: ['sessions'],
    queryFn: fetchSessions,
    retry: 2,
  });

  // Фильтруем мусорные сессии с нулевыми данными
  const filteredSessions = sessions?.filter(s =>
    hideEmpty ? (s.total_bubbles > 0 && s.duration_sec > 1) : true
  );

  // Берём последнюю отфильтрованную сессию как дефолтную
  const activeA = sessionAId || (filteredSessions && filteredSessions.length > 0 ? filteredSessions[filteredSessions.length - 1].start_time : null);
  const sessionA = sessions?.find(s => s.start_time === activeA);
  const sessionB = sessions?.find(s => s.start_time === sessionBId);

  const { data: detailsA } = useQuery({
    queryKey: ['details', activeA],
    queryFn: () => fetchDetails(activeA!),
    enabled: !!activeA,
  });

  const { data: detailsB } = useQuery({
    queryKey: ['details', sessionBId],
    queryFn: () => fetchDetails(sessionBId!),
    enabled: !!sessionBId,
  });

  const calcMetrics = (details: BubbleDetail[] | undefined, sessionInfo: Session | undefined) => {
    if (!details || details.length === 0) return {
      d32: 0, sb: 0, avgWobble: 0, avgMin: 0, avgRe: 0, avgDeltaV: 0, avgSpeedPx: 0, avgSpeedMm: 0,
      bsd: { x: [], y: [] }, topography: { x: [], y: [] },
      speedComparison: { labels: [], real: [], stokes: [] },
      reTimeline: { time: [], re: [] },
      alarms: [] as { type: string, msg: string, level: string }[],
      minDistribution: { high: 0, med: 0, low: 0 }
    };

    let sumD3 = 0, sumD2 = 0, sumVpx = 0, sumVmm = 0;
    let sumWobble = 0, sumMin = 0, sumRe = 0, sumDeltaV = 0;
    let maxR = 0, maxXPx = 0;

    const timeline: { t: number, re: number }[] = [];

    details.forEach(d => {
      const r_px = parseNumber(d.Radius_px);
      const r_mm = parseNumber(d.Radius_mm) || r_px;
      const v_px = parseNumber(d.Speed_px_s);
      const v_mm = parseNumber(d.Speed_mm_s);
      const x = parseNumber(d.X_px);
      const t = parseNumber(d.Time_ms);

      if (r_px > maxR) maxR = r_px;
      if (x > maxXPx) maxXPx = x;

      const diam = r_mm * 2;
      sumD3 += Math.pow(diam, 3);
      sumD2 += Math.pow(diam, 2);
      sumVpx += v_px;
      sumVmm += v_mm;
      sumWobble += parseNumber(d.Wobble_px);
      sumMin += parseNumber(d.Mineralization_Index);
      sumRe += parseNumber(d.Reynolds_Number);
      sumDeltaV += parseNumber(d.Delta_V_ms);

      if (t > 0) timeline.push({ t, re: parseNumber(d.Reynolds_Number) });
    });

    const n = details.length;
    const avgVpx = sumVpx / n;
    const avgVmm = sumVmm / n;
    const avgWobble = sumWobble / n;
    const avgMin = sumMin / n;
    const avgRe = sumRe / n;
    const avgDeltaV = sumDeltaV / n;
    const d32 = sumD2 > 0 ? sumD3 / sumD2 : 0;
    const sb = d32 > 0 ? (6 * avgVmm) / d32 : 0;

    // Histograms
    const bins = new Array(15).fill(0);
    const binS = maxR / 15 || 1;
    const xBins = new Array(20).fill(0);
    const xBinS = maxXPx / 20 || 1;

    const realByBin: number[][] = Array.from({ length: 12 }, () => []);
    const stokByBin: number[][] = Array.from({ length: 12 }, () => []);

    details.forEach(d => {
      const r = parseNumber(d.Radius_px);
      const idx = Math.min(14, Math.floor(r / binS));
      bins[idx]++;

      const x = parseNumber(d.X_px);
      const xIdx = Math.min(19, Math.floor(x / xBinS));
      xBins[xIdx]++;

      const bIdx = Math.min(11, Math.floor(r / (maxR / 12 || 1)));
      realByBin[bIdx].push(parseNumber(d.Speed_px_s));
      stokByBin[bIdx].push(parseNumber(d.V_Stokes_px) * (sessionInfo?.fps_actual || 30)); // V_stokes was per frame originally, scale to per sec
    });

    timeline.sort((a, b) => a.t - b.t);

    // Alarms calculation
    const alarms = [];
    if (avgRe > 700) alarms.push({ type: 'Re', msg: `Турбулентность: Re = ${avgRe.toFixed(0)} > 700`, level: 'danger' });
    if (sessionInfo && sessionInfo.rate_per_sec > 30) alarms.push({ type: 'Rate', msg: `Высокий поток: ${sessionInfo.rate_per_sec.toFixed(1)} пуз/с`, level: 'warn' });
    if (sessionInfo && sessionInfo.avg_radius_px < 3) alarms.push({ type: 'Size', msg: `Очень мелкие пузырьки (avg r=${sessionInfo.avg_radius_px.toFixed(1)}px)`, level: 'warn' });

    const minDistribution = { high: 0, med: 0, low: 0 };
    details.forEach(d => {
      const minIdx = parseNumber(d.Mineralization_Index);
      if (minIdx < 60) minDistribution.high++;
      else if (minIdx < 140) minDistribution.med++;
      else minDistribution.low++;
    });

    return {
      d32: d32.toFixed(3), sb: sb.toFixed(2),
      avgWobble: avgWobble.toFixed(1), avgMin: avgMin.toFixed(1),
      avgRe: avgRe.toFixed(0), avgDeltaV: avgDeltaV.toFixed(4),
      avgSpeedPx: avgVpx.toFixed(1), avgSpeedMm: avgVmm.toFixed(2),
      bsd: { x: bins.map((_, i) => `${(i * binS).toFixed(1)}`), y: bins },
      topography: { x: xBins.map((_, i) => `${(i * xBinS).toFixed(0)}`), y: xBins },
      speedComparison: {
        labels: bins.map((_, i) => `${(i * binS).toFixed(1)}`).slice(0, 12),
        real: realByBin.map(b => b.length > 0 ? b.reduce((a, c) => a + c, 0) / b.length : 0),
        stokes: stokByBin.map(b => b.length > 0 ? b.reduce((a, c) => a + c, 0) / b.length : 0)
      },
      reTimeline: {
        time: timeline.map(t => (t.t / 1000).toFixed(1)),
        re: timeline.map(t => t.re.toFixed(0))
      },
      alarms,
      minDistribution
    };
  };

  const metricsA = useMemo(() => calcMetrics(detailsA, sessionA), [detailsA, sessionA]);
  const metricsB = useMemo(() => calcMetrics(detailsB, sessionB), [detailsB, sessionB]);

  // --- PDF Export ---
  const handleExportPDF = async () => {
    setIsExporting(true);
    const el = document.getElementById('dashboard-content');
    if (el) {
      try {
        const canvas = await html2canvas(el, { backgroundColor: '#09090b', scale: 2 });
        const imgData = canvas.toDataURL('image/png');
        const pdf = new jsPDF('l', 'mm', 'a4');
        const pdfW = pdf.internal.pageSize.getWidth();
        const pdfH = (canvas.height * pdfW) / canvas.width;
        pdf.addImage(imgData, 'PNG', 0, 0, pdfW, pdfH);
        pdf.save(`Flotation_Report_${activeA}.pdf`);
      } catch (err) { console.error('Export failed', err); }
    }
    setIsExporting(false);
  };

  // Обработка ошибки подключения к API
  if (sessionsError) return (
    <div className="min-h-screen bg-black flex flex-col items-center justify-center gap-4 text-slate-400 font-mono">
      <div className="text-red-400 text-xl font-bold">⚠ API недоступен</div>
      <div className="text-sm text-slate-500">Проверьте: запущен ли bubble-api на порту 8000</div>
      <code className="bg-[#111] border border-[#222] rounded-lg px-4 py-2 text-xs text-cyan-400">http://localhost:8000/api/sessions</code>
      <button onClick={() => window.location.reload()} className="mt-2 px-4 py-2 bg-cyan-500/20 border border-cyan-500/30 rounded-lg text-cyan-400 hover:bg-cyan-500/30 transition-colors">Обновить</button>
    </div>
  );

  if (loadingSessions) {
    return (
      <div className="min-h-screen bg-background p-6 md:p-8 max-w-[1600px] mx-auto animate-pulse">
        {/* Header Skeleton */}
        <div className="flex justify-between items-center mb-10">
          <div className="h-12 w-64 bg-slate-200 rounded-lg"></div>
          <div className="h-12 w-96 bg-slate-200 rounded-xl"></div>
        </div>
        {/* KPI Cards Skeleton */}
        <div className="grid grid-cols-1 md:grid-cols-4 gap-5 mb-10">
          {[...Array(4)].map((_, i) => <div key={i} className="h-32 bg-white rounded-2xl shadow-sm border border-slate-100"></div>)}
        </div>
        {/* Live Panel Skeleton */}
        <div className="h-[400px] w-full bg-white rounded-2xl shadow-sm border border-slate-100 mb-10"></div>
        {/* Charts Skeleton */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          {[...Array(2)].map((_, i) => <div key={i} className="h-[320px] bg-white rounded-2xl shadow-sm border border-slate-100"></div>)}
        </div>
      </div>
    );
  }

  // CHART OPTIONS
  const axisColor = '#94a3b8';
  const labelColor = '#64748b';
  const gridLineColor = '#e2e8f0';

  const bsdOption = {
    backgroundColor: 'transparent', tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: '4%', right: '4%', bottom: '5%', top: '15%', containLabel: true },
    toolbox: { feature: { saveAsImage: { name: 'bsd', title: 'Скачать' }, dataView: { readOnly: true, title: 'Данные' } }, iconStyle: { borderColor: labelColor } },
    xAxis: { type: 'category', data: metricsA.bsd.x, axisLabel: { color: labelColor }, axisLine: { lineStyle: { color: axisColor } } },
    yAxis: { type: 'value', splitLine: { lineStyle: { color: gridLineColor, type: 'dashed' } }, axisLabel: { color: labelColor } },
    dataZoom: [{ type: 'inside' }, { type: 'slider', bottom: 0, textStyle: { color: labelColor } }],
    series: [
      { name: 'Сессия A', type: 'bar', data: metricsA.bsd.y, itemStyle: { color: '#3B82F6', borderRadius: [4, 4, 0, 0] }, barWidth: '40%' },
      ...(sessionBId ? [{ name: 'Сессия B', type: 'bar', data: metricsB.bsd.y, itemStyle: { color: '#0ea5e9', borderRadius: [4, 4, 0, 0] }, barWidth: '40%' }] : [])
    ]
  };

  const reOption = {
    backgroundColor: 'transparent', tooltip: { trigger: 'axis' },
    grid: { left: '4%', right: '4%', bottom: '5%', top: '15%', containLabel: true },
    toolbox: { feature: { dataZoom: { yAxisIndex: 'none', title: { zoom: 'Зум', back: 'Назад' } }, restore: { title: 'Сброс' }, saveAsImage: { title: 'Скачать' } }, iconStyle: { borderColor: labelColor } },
    xAxis: { type: 'category', data: metricsA.reTimeline.time, axisLabel: { color: labelColor, maxInterval: 10 }, name: 'Время (с)', nameTextStyle: { color: labelColor } },
    yAxis: { type: 'value', splitLine: { lineStyle: { color: gridLineColor, type: 'dashed' } }, axisLabel: { color: labelColor }, name: 'Re' },
    dataZoom: [{ type: 'inside' }, { type: 'slider', bottom: 0, textStyle: { color: labelColor } }],
    visualMap: {
      show: false, pieces: [{ gt: 0, lte: 700, color: '#10b981' }, { gt: 700, color: '#ef4444' }],
      outOfRange: { color: '#999' }
    },
    series: [{ name: 'Reynolds', type: 'line', data: metricsA.reTimeline.re, smooth: true, markLine: { data: [{ yAxis: 700, name: 'Турбулентность' }], lineStyle: { color: '#f59e0b', type: 'dashed' } } }]
  };

  const speedCompOption = {
    backgroundColor: 'transparent', tooltip: { trigger: 'axis' }, legend: { textStyle: { color: labelColor }, top: 0 },
    grid: { left: '4%', right: '4%', bottom: '5%', top: '15%', containLabel: true },
    toolbox: { feature: { saveAsImage: { title: 'Скачать' } }, iconStyle: { borderColor: labelColor } },
    xAxis: { type: 'category', data: metricsA.speedComparison.labels, axisLabel: { color: labelColor } },
    yAxis: { type: 'value', splitLine: { lineStyle: { color: gridLineColor, type: 'dashed' } }, axisLabel: { color: labelColor } },
    dataZoom: [{ type: 'inside' }, { type: 'slider', bottom: 0, textStyle: { color: labelColor } }],
    series: [
      { name: 'Реальная (px/s)', type: 'line', smooth: true, symbolSize: 6, data: metricsA.speedComparison.real, itemStyle: { color: '#3B82F6' }, lineStyle: { width: 3 } },
      { name: 'Stokes (px/s)', type: 'line', smooth: true, symbolSize: 6, data: metricsA.speedComparison.stokes, itemStyle: { color: '#10b981' }, lineStyle: { width: 2, type: 'dashed' } }
    ]
  };

  const scatterOption = {
    backgroundColor: 'transparent',
    tooltip: { formatter: (p: { value: number[] }) => `Скорость: ${p.value[0]}<br/>Радиус: ${p.value[1]}` },
    visualMap: { min: 0, max: 200, dimension: 2, orient: 'horizontal', right: 10, top: 0, text: ['Руда', 'Пустой'], textStyle: { color: labelColor, fontSize: 10 }, inRange: { color: ['#e2e8f0', '#3b82f6', '#f59e0b', '#dc2626'] } },
    grid: { left: '5%', right: '5%', bottom: '8%', top: '18%', containLabel: true },
    toolbox: { feature: { dataZoom: { yAxisIndex: 'none', title: { zoom: 'Зум', back: 'Назад' } }, restore: { title: 'Сброс' }, saveAsImage: { title: 'Скачать' } }, iconStyle: { borderColor: labelColor } },
    xAxis: { type: 'value', name: 'Скорость (px/s)', splitLine: { lineStyle: { color: gridLineColor, type: 'dashed' } }, axisLabel: { color: labelColor } },
    yAxis: { type: 'value', name: 'Радиус (px)', splitLine: { lineStyle: { color: gridLineColor, type: 'dashed' } }, axisLabel: { color: labelColor } },
    dataZoom: [{ type: 'inside', xAxisIndex: 0, yAxisIndex: 0 }, { type: 'slider', xAxisIndex: 0, bottom: 0, textStyle: { color: labelColor } }, { type: 'slider', yAxisIndex: 0, right: 0 }],
    // Фикс: передаём пустой массив если данных нет, иначе ECharts крашится
    series: [{ name: 'A', symbolSize: (d: number[]) => Math.min(12, Math.max(3, (d[3] || 1) * 1.5)), data: detailsA?.map(d => [parseNumber(d.Speed_px_s), parseNumber(d.Radius_px), parseNumber(d.Mineralization_Index), parseNumber(d.Wobble_px)]) || [], type: 'scatter', itemStyle: { opacity: 0.7 } }]
  };

  const minPieOption = {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    legend: { bottom: '0', textStyle: { color: labelColor } },
    series: [
      {
        name: 'Минерализация', type: 'pie', radius: ['40%', '70%'],
        avoidLabelOverlap: false,
        itemStyle: { borderRadius: 10, borderColor: '#fff', borderWidth: 2 },
        label: { show: false, position: 'center' },
        emphasis: { label: { show: true, fontSize: 16, fontWeight: 'bold' } },
        labelLine: { show: false },
        data: [
          { value: metricsA.minDistribution.high, name: 'Высокая (Руда)', itemStyle: { color: '#D97706' } },
          { value: metricsA.minDistribution.med, name: 'Средняя', itemStyle: { color: '#3B82F6' } },
          { value: metricsA.minDistribution.low, name: 'Низкая (Пустой)', itemStyle: { color: '#94A3B8' } }
        ]
      }
    ]
  };

  return (
    <div className="min-h-screen bg-background text-foreground selection:bg-blue-100 overflow-x-hidden font-sans">

      <div id="dashboard-content" className="p-6 md:p-8 max-w-[1600px] mx-auto bg-background">

        {/* HEADER */}
        <motion.header initial={{ opacity: 0, y: -20 }} animate={{ opacity: 1, y: 0 }} className="flex flex-col xl:flex-row justify-between items-start xl:items-center mb-10 gap-6">
          <div>
            <div className="flex items-center gap-3 mb-2">
              <div className="p-2 bg-blue-50 rounded-lg border border-blue-100"><ScanEye className="w-6 h-6 text-primary" /></div>
              <h1 className="text-3xl md:text-4xl font-bold tracking-tight text-foreground font-sans">Bubble Tracker <span className="text-primary font-black">PRO</span></h1>
            </div>
            <p className="text-slate-500 text-sm flex items-center gap-2 font-medium">
              <span className="w-2 h-2 rounded-full bg-emerald-500 shadow-sm"></span>
              Industrial Flotation Analytics • {detailsA?.length || 0} track points
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-3 bg-white border border-border p-2 rounded-2xl shadow-sm">
            {/* Переключатель фильтра пустых сессий */}
            <button onClick={() => setHideEmpty(h => !h)}
              className={`px-3 py-1.5 rounded-lg text-xs font-semibold border transition-all cursor-pointer ${
                hideEmpty ? 'bg-blue-50 border-blue-200 text-primary' : 'bg-slate-50 border-slate-200 text-slate-500'
              }`}>
              {hideEmpty ? '🔍 Только с данными' : '👁 Все сессии'}
            </button>
            <div className="w-px h-8 bg-slate-200"></div>
            <div className="flex flex-col px-3 py-1">
              <label className="text-[10px] text-slate-500 uppercase font-bold tracking-widest mb-1 flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-primary"></span> Сессия A (Main)</label>
              <select className="bg-transparent text-sm font-semibold text-foreground outline-none w-48 cursor-pointer" value={activeA || ''} onChange={(e) => setSessionAId(e.target.value)}>
                {filteredSessions?.map(s => <option className="bg-white" key={s.start_time} value={s.start_time}>{s.start_time}</option>)}
              </select>
            </div>
            <div className="w-px h-10 bg-slate-200"></div>
            <div className="flex flex-col px-3 py-1">
              <label className="text-[10px] text-slate-500 uppercase font-bold tracking-widest mb-1 flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-cyan-500"></span> Сравнение B</label>
              <select className="bg-transparent text-sm font-semibold text-foreground outline-none w-48 cursor-pointer" value={sessionBId || ''} onChange={(e) => setSessionBId(e.target.value || null)}>
                <option className="bg-white" value="">— Не выбрано —</option>
                {filteredSessions?.map(s => <option className="bg-white" key={s.start_time + "_B"} value={s.start_time}>{s.start_time}</option>)}
              </select>
            </div>
            <button onClick={handleExportPDF} disabled={isExporting} className="ml-2 px-4 py-2.5 bg-slate-50 hover:bg-blue-50 border border-slate-200 hover:border-primary rounded-xl transition-all flex items-center gap-2 text-sm font-semibold text-primary cursor-pointer">
              {isExporting ? <span className="animate-pulse">PDF...</span> : <><FileDown className="w-4 h-4" /> Экспорт</>}
            </button>
          </div>
        </motion.header>

        {/* ALERTS SECTION */}
        {metricsA.alarms.length > 0 && (
          <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} className="mb-8 flex flex-col gap-3">
            {metricsA.alarms.map((a, i) => (
              <div key={i} className={`flex items-center gap-3 p-4 rounded-xl border font-sans ${a.level === 'danger' ? 'bg-red-50 border-red-200 text-red-600' : 'bg-amber-50 border-amber-200 text-amber-600'}`}>
                {a.level === 'danger' ? <AlertTriangle className="w-5 h-5" /> : <AlertCircle className="w-5 h-5" />}
                <span className="font-semibold text-sm">{a.msg}</span>
              </div>
            ))}
          </motion.div>
        )}

        {/* 🎥 LIVE VIDEO STREAM — показываем всегда (заглушка если трекер не запущен) */}
        <LiveVideoPanel />

        {/* METRICS ROW 1 */}
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-5 mb-5">
          <MetricCard title="Пузырьков" value={sessionA?.total_bubbles || 0} icon={Droplets} delay={0.1}
            subtext="Подсчитано за сессию" />
          <MetricCard title="Поток Аэрации" value={sessionA?.rate_per_sec || 0} unit="пуз/с" icon={Activity} delay={0.15}
            subtext="Скорость генерации" />
          <MetricCard title="Диам. Заутера D₃₂" value={metricsA.d32} unit="мм" icon={Target} delay={0.2}
            subtext="Отношение объёма к поверхности" />
          <MetricCard title="Средний Рейнольдс" value={metricsA.avgRe} icon={Beaker} delay={0.25}
            subtext={Number(metricsA.avgRe) > 700 ? '⚠️ Турбулентный' : 'Ламинарный'} />
        </div>

        {/* METRICS ROW 2 */}
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-5 mb-10">
          <MetricCard title="Средняя Скорость" value={metricsA.avgSpeedPx} unit="px/s" icon={Gauge} delay={0.3}
            subtext={`Или ${metricsA.avgSpeedMm} мм/с`} />
          <MetricCard title="Поверхностный поток Sb" value={metricsA.sb || 0} unit="с⁻¹" icon={Layers} delay={0.35}
            subtext="Несущая способность" />
          <MetricCard title="Минерализация" value={metricsA.avgMin} icon={ScanEye} delay={0.4}
            subtext="Индекс (0=Руда, 200=Пусто)" />
          <MetricCard title="Длительность" value={sessionA?.duration_sec || 0} unit="с" icon={Activity} delay={0.45}
            subtext={`${sessionA?.res_w}x${sessionA?.res_h} @ ${sessionA?.fps_actual?.toFixed(1)} FPS`} />
        </div>

        {/* DETECTIONS STRIP */}
        <div className="flex flex-wrap gap-4 mb-10 bg-white border border-border p-4 rounded-2xl items-center shadow-sm">
          <div className="text-xs text-slate-500 uppercase font-bold tracking-widest mr-4 font-sans">Методы детекции:</div>
          <div className="flex gap-6">
            <span className="text-sm text-slate-600 font-semibold"><span className="text-primary font-mono bg-blue-50 px-1 rounded">{sessionA?.det_hough || 0}</span> Hough</span>
            <span className="text-sm text-slate-600 font-semibold"><span className="text-secondary font-mono bg-blue-50 px-1 rounded">{sessionA?.det_contour || 0}</span> Contour</span>
            <span className="text-sm text-slate-600 font-semibold"><span className="text-accent font-mono bg-amber-50 px-1 rounded">{sessionA?.det_blob || 0}</span> Blob</span>
          </div>
          <div className="w-px h-6 bg-slate-200 mx-2"></div>
          <div className="flex gap-6">
            <span className="text-sm flex items-center gap-1 text-slate-600 font-semibold">Convexity Split: {sessionA?.use_convex ? <CheckCircle2 className="w-4 h-4 text-emerald-500" /> : 'Выкл'}</span>
            <span className="text-sm flex items-center gap-1 text-slate-600 font-semibold">Watershed: {sessionA?.use_watershed ? <CheckCircle2 className="w-4 h-4 text-emerald-500" /> : 'Выкл'}</span>
          </div>
        </div>

        {/* CHARTS GRID */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
          <motion.div initial={{ opacity: 0, scale: 0.98 }} animate={{ opacity: 1, scale: 1 }} transition={{ delay: 0.5 }} className="card card-hoverable h-[400px]">
            <h3 className="text-base font-bold text-foreground flex items-center gap-2 mb-2 font-sans"><BarChart3 className="w-5 h-5 text-primary" /> BSD (Распределение размеров)</h3>
            <ReactECharts option={bsdOption} style={{ height: '320px', width: '100%' }} />
          </motion.div>

          <motion.div initial={{ opacity: 0, scale: 0.98 }} animate={{ opacity: 1, scale: 1 }} transition={{ delay: 0.6 }} className="card card-hoverable h-[400px]">
            <h3 className="text-base font-bold text-foreground flex items-center gap-2 mb-2 font-sans"><Gauge className="w-5 h-5 text-primary" /> Скорость всплытия (Реальная vs Stokes)</h3>
            <ReactECharts option={speedCompOption} style={{ height: '320px', width: '100%' }} />
          </motion.div>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-10">
          <motion.div initial={{ opacity: 0, scale: 0.98 }} animate={{ opacity: 1, scale: 1 }} transition={{ delay: 0.7 }} className="card card-hoverable h-[400px]">
            <h3 className="text-base font-bold text-foreground flex items-center gap-2 mb-2 font-sans"><Beaker className="w-5 h-5 text-primary" /> Число Рейнольдса во времени</h3>
            <ReactECharts option={reOption} style={{ height: '320px', width: '100%' }} />
          </motion.div>

          <motion.div initial={{ opacity: 0, scale: 0.98 }} animate={{ opacity: 1, scale: 1 }} transition={{ delay: 0.8 }} className="card card-hoverable h-[400px]">
            <h3 className="text-base font-bold text-foreground flex items-center gap-2 mb-2 font-sans"><ScanEye className="w-5 h-5 text-primary" /> Тепловая карта: Скорость, Радиус, Минерализация</h3>
            <ReactECharts option={scatterOption} style={{ height: '320px', width: '100%' }} />
          </motion.div>
        </div>

        {/* ROW 3: Doughnut & Table */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 mb-10">
          <motion.div initial={{ opacity: 0, scale: 0.98 }} animate={{ opacity: 1, scale: 1 }} transition={{ delay: 0.9 }} className="card card-hoverable h-[400px]">
            <h3 className="text-base font-bold text-foreground flex items-center gap-2 mb-2 font-sans"><Target className="w-5 h-5 text-primary" /> Пропорции Минерализации</h3>
            <ReactECharts option={minPieOption} style={{ height: '320px', width: '100%' }} />
          </motion.div>

          <motion.div initial={{ opacity: 0, scale: 0.98 }} animate={{ opacity: 1, scale: 1 }} transition={{ delay: 1.0 }} className="card card-hoverable h-[400px] lg:col-span-2 overflow-hidden flex flex-col p-0 border border-border">
            <div className="px-6 py-4 border-b border-border bg-white flex justify-between items-center z-20">
              <h3 className="text-base font-bold text-foreground font-sans flex items-center gap-2"><Layers className="w-5 h-5 text-primary"/> Топ Детектированных Пузырьков</h3>
              <span className="text-xs text-slate-500 font-mono bg-slate-50 px-2 py-1 rounded border border-slate-200 shadow-sm">По размеру (10)</span>
            </div>
            <div className="overflow-auto flex-1 p-0 custom-scrollbar">
              <table className="w-full text-left border-collapse min-w-[500px]">
                <thead className="sticky top-0 bg-slate-50/95 backdrop-blur z-10 shadow-sm">
                  <tr className="text-[11px] text-slate-500 uppercase tracking-widest font-sans border-b border-border">
                    <th className="px-6 py-3 font-semibold">ID / Кадр</th>
                    <th className="px-6 py-3 font-semibold text-right">Радиус (px)</th>
                    <th className="px-6 py-3 font-semibold text-right">Скорость (px/s)</th>
                    <th className="px-6 py-3 font-semibold">Минерализация</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 bg-white">
                  {detailsA?.slice().sort((a,b) => parseNumber(b.Radius_px) - parseNumber(a.Radius_px)).slice(0, 10).map((d, i) => (
                    <tr key={i} className="hover:bg-blue-50 transition-colors font-mono text-sm text-slate-700 group cursor-pointer">
                      <td className="px-6 py-3">
                        <div className="font-bold text-primary">#{d.ID}</div>
                        <div className="text-[10px] text-slate-400 font-sans group-hover:text-primary transition-colors">Кадр {d.Frame}</div>
                      </td>
                      <td className="px-6 py-3 font-semibold text-right">{parseNumber(d.Radius_px).toFixed(1)}</td>
                      <td className="px-6 py-3 font-semibold text-right">{parseNumber(d.Speed_px_s).toFixed(1)}</td>
                      <td className="px-6 py-3">
                        <div className="flex items-center gap-3">
                          <div className="flex-1 max-w-[100px] h-2 bg-slate-100 rounded-full overflow-hidden shadow-inner">
                            <div className={`h-full ${parseNumber(d.Mineralization_Index) < 60 ? 'bg-accent' : parseNumber(d.Mineralization_Index) < 140 ? 'bg-secondary' : 'bg-slate-400'}`} style={{ width: `${Math.max(0, Math.min(100, 100 - (parseNumber(d.Mineralization_Index)/2)))}%` }}></div>
                          </div>
                          <span className="text-xs text-slate-500 w-6 text-right font-medium">{parseNumber(d.Mineralization_Index).toFixed(0)}</span>
                        </div>
                      </td>
                    </tr>
                  ))}
                  {(!detailsA || detailsA.length === 0) && (
                    <tr>
                      <td colSpan={4} className="p-8 text-center text-slate-500 font-sans bg-white">Нет данных для отображения</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </motion.div>
        </div>

      </div>
    </div>
  );
}
