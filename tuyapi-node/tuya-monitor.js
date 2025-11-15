const TuyAPI = require('tuyapi');
const chalk = require('chalk').default;

// Configuration - REPLACE WITH YOUR VALUES
const deviceConfig = {
  id: "bfc3b6613aef942483cwst",      // Tuya device ID
  key: "+xsL0^>f~Q?)Xh&J",           // Tuya local key
  ip: "192.168.68.100",             // Local IP (optional, but recommended)
  version: '3.5'                     // Device protocol version: 3.1, 3.3, or 3.5
};

// Initialize device
const device = new TuyAPI({
  ...deviceConfig,
  issueRefreshOnConnect: true,
  issueGetOnConnect: true,
  connectionTimeout: 2000,
  persistentConnection: false
});

// Global error listener (prevents crash on unplug)
device.on('error', (err) => {
  console.log(chalk.red('Device emitted an error:'), err.message);
  handleConnectionError(err);
  lastValues.status = 'disconnected';
  lastValues.timestamp = new Date();
  console.log(formatStatus(lastValues));
});

let monitoringActive = true;
let connectionAttempts = 0;

let lastValues = {
  power: 0,
  voltage: 0,
  current: 0,
  timestamp: null,
  status: 'disconnected'
};

function formatValue(value, unit) {
  const formatted = value.toFixed(unit === 'A' ? 3 : unit === 'V' ? 1 : 2);
  if (unit === 'W') return chalk.green(`${formatted}${unit}`);
  if (unit === 'V') return chalk.blue(`${formatted}${unit}`);
  if (unit === 'A') return chalk.red(`${formatted}${unit}`);
  return formatted;
}

function formatStatus(data) {
  const time = data.timestamp?.toLocaleTimeString() || '--:--:--';
  const statusIcon = data.status === 'connected' ? chalk.green('✓') :
                     data.status === 'error' ? chalk.red('✖') : chalk.yellow('⌛');
  return [
    chalk.gray(`[${time}]`),
    'Power:', formatValue(data.power, 'W'),
    '| Voltage:', formatValue(data.voltage, 'V'),
    '| Current:', formatValue(data.current, 'A'),
    statusIcon,
    data.status === 'error' ? chalk.red('(retrying)') : ''
  ].join(' ');
}

// Handle connection errors - unlimited retries, no maxAttempts limit
function handleConnectionError(error) {
  connectionAttempts++;
  lastValues.status = 'error';

  const shortMsg = error?.message || error.toString();
  console.log(chalk.yellow(`Connection attempt ${connectionAttempts}: ${shortMsg}`));
}

async function forceRefresh() {
  try {
    if (device.isConnected) {
      try {
        await device.disconnect();
      } catch (disconnectError) {
        console.log(chalk.yellow(`Disconnect warning: ${disconnectError.message}`));
      }
    }

    if (!device.ip) {
      try {
        await device.find();
      } catch (findError) {
        handleConnectionError(findError);
        lastValues.status = 'disconnected';
        lastValues.timestamp = new Date();
        console.log(formatStatus(lastValues));
        return;
      }
    }

    try {
      await device.connect();
    } catch (connectError) {
      handleConnectionError(connectError);
      lastValues.status = 'disconnected';
      lastValues.timestamp = new Date();
      console.log(formatStatus(lastValues));
      return;
    }

    connectionAttempts = 0; // Reset after successful connection
    lastValues.status = 'connected';

    let data;
    try {
      data = await device.get({ schema: true, force: true });
    } catch (getError) {
      handleConnectionError(getError);
      lastValues.status = 'error';
      lastValues.timestamp = new Date();
      console.log(formatStatus(lastValues));
      return;
    }

    const dps = data.dps || {};
    const newValues = {
      power: dps['19'] !== undefined ? dps['19'] / 10 : lastValues.power,
      voltage: dps['20'] !== undefined ? dps['20'] / 10 : lastValues.voltage,
      current: dps['18'] !== undefined ? dps['18'] / 1000 : lastValues.current,
      timestamp: new Date(),
      status: 'connected'
    };

    if (JSON.stringify(newValues) !== JSON.stringify(lastValues)) {
      lastValues = newValues;
      console.log(formatStatus(lastValues));
    }

  } catch (unexpectedError) {
    handleConnectionError(unexpectedError);
    lastValues.status = 'error';
    lastValues.timestamp = new Date();
    console.log(formatStatus(lastValues));
  } finally {
    try {
      if (device.isConnected) {
        await device.disconnect();
      }
    } catch (cleanupError) {
      console.log(chalk.yellow(`Cleanup warning: ${cleanupError.message}`));
    }
  }
}

async function monitor() {
  console.log(chalk.bold.cyan('Tuya Smart Plug Monitor - Resilient Version'));
  console.log(chalk.gray('Press Ctrl+C to exit\n'));

  await forceRefresh();

  const interval = setInterval(async () => {
    if (monitoringActive) {
      await forceRefresh();
    }
  }, 2000); // every 5 seconds

  process.on('SIGINT', async () => {
    monitoringActive = false;
    clearInterval(interval);
    try {
      if (device.isConnected) {
        await device.disconnect();
      }
    } catch (error) {
      console.log(chalk.yellow(`Exit cleanup error: ${error.message}`));
    }
    console.log(chalk.cyan('\nMonitoring stopped. Goodbye!'));
    process.exit();
  });
}

(async () => {
  try {
    await monitor();
  } catch (error) {
    console.error(chalk.red('Fatal monitoring error:'), error);
    process.exit(1);
  }
})();