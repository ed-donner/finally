interface SparklineProps {
  values: number[];
  stroke?: string;
  height?: number;
}

export const Sparkline = ({ values, stroke = '#209dd7', height = 34 }: SparklineProps) => {
  if (values.length < 2) {
    return <div className="h-8 w-full rounded bg-terminal-panelAlt/40" />;
  }

  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = Math.max(max - min, 0.0001);
  const width = 180;

  const points = values
    .map((value, index) => {
      const x = (index / (values.length - 1)) * width;
      const y = height - ((value - min) / range) * height;
      return `${x},${y}`;
    })
    .join(' ');

  return (
    <svg width="100%" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" className="h-8 w-full">
      <polyline fill="none" stroke={stroke} strokeWidth="2" points={points} />
    </svg>
  );
};
