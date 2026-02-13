interface SparklineProps {
  values: number[];
  stroke?: string;
  height?: number;
  className?: string;
}

export const Sparkline = ({ values, stroke = '#209dd7', height = 34, className = 'h-8' }: SparklineProps) => {
  if (values.length === 0) {
    return <div className={`w-full rounded bg-terminal-panelAlt/40 ${className}`} />;
  }
  const normalizedValues = values.length === 1 ? [values[0], values[0]] : values;

  const min = Math.min(...normalizedValues);
  const max = Math.max(...normalizedValues);
  const range = Math.max(max - min, 0.0001);
  const width = 180;

  const points = normalizedValues
    .map((value, index) => {
      const x = (index / (normalizedValues.length - 1)) * width;
      const y = height - ((value - min) / range) * height;
      return `${x},${y}`;
    })
    .join(' ');

  return (
    <svg width="100%" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" className={`w-full ${className}`}>
      <polyline fill="none" stroke={stroke} strokeWidth="2" points={points} />
    </svg>
  );
};
