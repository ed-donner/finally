export const countGridColumns = (gridTemplateColumns: string): number => {
  const template = gridTemplateColumns.trim();
  if (!template || template === 'none') {
    return 0;
  }

  const topLevelTracks: string[] = [];
  let token = '';
  let depth = 0;

  for (const char of template) {
    if (char === '(') {
      depth += 1;
      token += char;
      continue;
    }

    if (char === ')') {
      depth = Math.max(depth - 1, 0);
      token += char;
      continue;
    }

    if (/\s/.test(char) && depth === 0) {
      if (token) {
        topLevelTracks.push(token);
        token = '';
      }
      continue;
    }

    token += char;
  }

  if (token) {
    topLevelTracks.push(token);
  }

  return topLevelTracks.reduce((count, track) => {
    const repeatMatch = track.match(/^repeat\(\s*(\d+)\s*,/i);
    if (repeatMatch) {
      return count + Number.parseInt(repeatMatch[1], 10);
    }
    return count + 1;
  }, 0);
};
