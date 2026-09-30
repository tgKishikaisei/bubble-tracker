/**
 * LiveSparkChart.tsx
 * Мини-график скорости и радиуса — заменяет OpenCV-оверлей.
 * Данные приходят через WebSocket (live_stats.speed_hist / radius_hist).
 * React.memo → ре-рендер только при изменении данных, без мигания.
 */
import { memo, useMemo } from 'react';

interface LiveSparkChartProps {
  speedHist:  number[];
  radiusHist: number[];
}

// Нормализует массив [0..1] по собственному максимуму
function normalize(arr: number[]): number[] {
  const mx = Math.max(...arr, 0.001);
  return arr.map(v => v / mx);
}

// Строит SVG polyline points из нормализованного массива
function toPoints(norm: number[], w: number, h: number, padY = 4): string {
  if (norm.length < 2) return '';
  return norm
    .map((v, i) => {
      const x = (i / (norm.length - 1)) * w;
      const y = h - padY - v * (h - padY * 2);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(' ');
}

const W = 240;
const H = 72;

const LiveSparkChart = memo(({ speedHist, radiusHist }: LiveSparkChartProps) => {
  const normSpeed  = useMemo(() => normalize(speedHist),  [speedHist]);
  const normRadius = useMemo(() => normalize(radiusHist), [radiusHist]);

  const ptsSpeed  = useMemo(() => toPoints(normSpeed,  W, H), [normSpeed]);
  const ptsRadius = useMemo(() => toPoints(normRadius, W, H), [normRadius]);

  // Последние значения для лейблов
  const lastSpeed  = speedHist.at(-1)  ?? 0;
  const lastRadius = radiusHist.at(-1) ?? 0;

  if (speedHist.length < 2) {
    return (
      <div className="flex items-center justify-center h-20 text-xs text-slate-600 font-mono">
        Ожидание данных...
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-1">
      {/* Заголовок */}
      <div className="flex items-center justify-between px-1">
        <span className="text-[10px] text-slate-500 uppercase font-bold tracking-widest font-sans">
          Live График
        </span>
        <div className="flex items-center gap-3 text-[10px] font-mono font-semibold">
          <span className="flex items-center gap-1">
            <span className="w-2 h-0.5 bg-primary inline-block rounded" />
            <span className="text-primary">v={lastSpeed.toFixed(1)}</span>
          </span>
          <span className="flex items-center gap-1">
            <span className="w-2 h-0.5 bg-accent inline-block rounded" />
            <span className="text-accent">r={lastRadius.toFixed(1)}</span>
          </span>
        </div>
      </div>

      {/* SVG График */}
      <div
        className="rounded-xl overflow-hidden border border-slate-200 bg-slate-50 shadow-inner"
      >
        <svg
          width="100%"
          viewBox={`0 0 ${W} ${H}`}
          preserveAspectRatio="none"
          style={{ display: 'block', height: `${H}px` }}
        >
          {/* Сетка */}
          {[0.25, 0.5, 0.75].map(t => (
            <line
              key={t}
              x1={0} y1={H * (1 - t)}
              x2={W} y2={H * (1 - t)}
              stroke="#E2E8F0" strokeWidth={1} strokeDasharray="2 2"
            />
          ))}

          {/* Градиент-заливка скорости */}
          <defs>
            <linearGradient id="gradSpeed" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%"   stopColor="#1E40AF" stopOpacity={0.15} />
              <stop offset="100%" stopColor="#1E40AF" stopOpacity={0}    />
            </linearGradient>
            <linearGradient id="gradRadius" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%"   stopColor="#D97706" stopOpacity={0.15} />
              <stop offset="100%" stopColor="#D97706" stopOpacity={0}   />
            </linearGradient>
          </defs>

          {/* Заливка скорость */}
          {ptsSpeed && (
            <polygon
              points={`0,${H} ${ptsSpeed} ${W},${H}`}
              fill="url(#gradSpeed)"
            />
          )}

          {/* Линия скорости */}
          {ptsSpeed && (
            <polyline
              points={ptsSpeed}
              fill="none"
              stroke="#1E40AF"
              strokeWidth={1.5}
              strokeLinejoin="round"
              strokeLinecap="round"
            />
          )}

          {/* Заливка радиус */}
          {ptsRadius && (
            <polygon
              points={`0,${H} ${ptsRadius} ${W},${H}`}
              fill="url(#gradRadius)"
            />
          )}

          {/* Линия радиуса */}
          {ptsRadius && (
            <polyline
              points={ptsRadius}
              fill="none"
              stroke="#D97706"
              strokeWidth={1.5}
              strokeLinejoin="round"
              strokeLinecap="round"
              strokeDasharray="3 2"
            />
          )}

          {/* Последняя точка скорости */}
          {ptsSpeed && normSpeed.length > 0 && (
            <circle
              cx={W}
              cy={H - 4 - normSpeed.at(-1)! * (H - 8)}
              r={3}
              fill="#1E40AF"
            />
          )}
        </svg>
      </div>
    </div>
  );
});

export default LiveSparkChart;
