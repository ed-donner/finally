function executeHook() {
  if (hasRun) return;
  hasRun = true;

  const projectRoot = path.join(__dirname, '..', '..');
  const logDir = path.join(projectRoot, 'planning');
  if (!fs.existsSync(logDir)) fs.mkdirSync(logDir, { recursive: true });

  const logFile = path.join(logDir, 'agy-review.log');

  // Write the "hook fired" timestamp line first
  const timestamp = new Date().toISOString();
  fs.appendFileSync(logFile, `\n=== hook-fired ${timestamp} ===\n`);

  const logFd = fs.openSync(logFile, 'a'); // 'a' = append, so this doesn't wipe the timestamp line above

  const prompt = "Write a REVIEW.md of this project's last file changes at planning/REVIEW.md";

  try {
    execFileSync('agy', ['-p', prompt, '--dangerously-skip-permissions'], {
      cwd: projectRoot,
      stdio: ['ignore', logFd, logFd],
      windowsHide: true,
      shell: true,
      timeout: 170000
    });
  } catch (err) {
    fs.writeSync(logFd, `\n[hook error] ${err.message}\n`);
  } finally {
    fs.closeSync(logFd);
  }

  console.log(JSON.stringify({ decision: 'allow' }));
  process.exit(0);
}