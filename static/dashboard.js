const toggleBtn = document.getElementById('toggle-btn');
const video = document.getElementById('webcam');
const alertOverlay = document.getElementById('alert-overlay');
const alertDetail = document.getElementById('alert-detail');
const connectionDot = document.getElementById('connection-dot');
const connectionText = document.getElementById('connection-text');
const statsBtn = document.getElementById('stats-btn');
const logoutBtn = document.getElementById('logout-btn');
const userLabel = document.getElementById('user-label');

let isRunning = false;
let ws = null;
let stream = null;
let intervalId = null;
let alertTimeout = null;

// Set by startFrameStream so the WebSocket message handler can release the
// capture slot once the server has answered.
let frameSlotRelease = null;
let stopWatchdog = null;

// Bounds the frame send rate. Inference takes longer than this on CPU, and the
// client now waits for each response, so this acts as a ceiling, not a floor.
const FRAME_INTERVAL_MS = 200;
const JPEG_QUALITY = 0.6;

// How long the alert overlay stays up after the last alerting frame. Needs to
// cover a few missed frames (shadow, hand blocking the lens) without making
// the overlay feel stuck once the phone is genuinely put away.
const ALERT_LINGER_MS = 3000;

// Upper bound on how long one frame may occupy the capture slot. Generous
// relative to inference latency, but bounded so a lost reply cannot stop the
// feed permanently.
const FRAME_TIMEOUT_MS = 5000;

// --- Session gate -------------------------------------------------------
// The dashboard is only useful with a valid session, so check before wiring up
// the camera. The WebSocket handshake is also cookie-gated server-side.
(async function requireSession() {
    try {
        const res = await fetch('/api/me', { credentials: 'same-origin' });
        if (!res.ok) {
            window.location.replace('index.html');
            return false;
        }
        const data = await res.json();
        if (userLabel && data.user) userLabel.textContent = data.user.username;
    } catch {
        window.location.replace('index.html');
        return false;
    }
    return true;
})();

// --- Toggle guard --------------------------------------------------------

toggleBtn?.addEventListener('click', async () => {
    if (!isRunning) {
        await startGuard();
    } else {
        stopGuard();
    }
});

async function startGuard() {
    try {
        stream = await navigator.mediaDevices.getUserMedia({ video: { width: 640, height: 480 } });
        video.srcObject = stream;

        // Wait for the video to have real dimensions, otherwise the first
        // canvas drawImage samples a 0x0 frame.
        await new Promise((resolve) => {
            if (video.videoWidth) return resolve();
            video.addEventListener('loadedmetadata', resolve, { once: true });
        });

        connectWebSocket();

        isRunning = true;
        toggleBtn.classList.add('active');
        toggleBtn.querySelector('span').textContent = 'Guard Active';
    } catch (err) {
        console.error('Error starting:', err);
        alert('Could not access camera.');
    }
}

function stopGuard() {
    if (intervalId) clearInterval(intervalId);
    intervalId = null;
    stopWatchdog?.();
    frameSlotRelease = null;

    if (ws) {
        ws.onclose = null;
        ws.close();
        ws = null;
    }

    if (stream) {
        stream.getTracks().forEach((track) => track.stop());
        stream = null;
        video.srcObject = null;
    }

    isRunning = false;
    toggleBtn?.classList.remove('active');
    if (toggleBtn) toggleBtn.querySelector('span').textContent = 'Turn On Guard';
    connectionDot?.classList.remove('active');
    if (connectionText) connectionText.textContent = 'Disconnected';
    alertOverlay?.classList.remove('visible');
}

// --- WebSocket -----------------------------------------------------------

function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const wsUrl = `${protocol}://${window.location.host}/ws/detect`;

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
        console.log('Connected to server');
        connectionDot?.classList.add('active');
        if (connectionText) connectionText.textContent = 'Live Monitoring';
        startFrameStream();
    };

    ws.onmessage = (event) => {
        // Release the capture slot first, even for malformed replies, so one
        // bad message cannot wedge the stream.
        frameSlotRelease?.();

        let data;
        try {
            data = JSON.parse(event.data);
        } catch {
            console.warn('Unparseable frame from server');
            return;
        }

        if (data.status === 'error') {
            console.warn('Server error:', data.error);
            return;
        }

        // Server sends {status, alert, detections:{found, confidence, ...}}.
        if (data.alert) {
            showAlert(data.detections);
        } else if (data.detections && data.detections.reason) {
            // A miss carries a reason and a tuning hint. Logged rather than
            // swallowed, so "it didn't alert" is diagnosable from the console.
            console.debug(
                `No alert (${data.detections.reason}) ` +
                `conf=${data.detections.confidence} rel_area=${data.detections.rel_area}. ` +
                `${data.detections.hint || ''}`
            );
        }
    };

    // A rejected handshake never reaches onclose with a code: the server
    // refuses the upgrade at the HTTP layer, so the socket errors out instead.
    ws.onerror = () => {
        console.error('WebSocket error');
    };

    ws.onclose = (event) => {
        console.log('Disconnected', event.code);
        connectionDot?.classList.remove('active');

        const sessionLost = event.code === 4401 || event.code === 1006;
        if (connectionText) {
            connectionText.textContent = sessionLost ? 'Session expired' : 'Disconnected';
        }
        if (isRunning) stopGuard();
        if (sessionLost) window.location.replace('index.html');
    };
}

function startFrameStream() {
    const canvas = document.createElement('canvas');
    const ctx = canvas.getContext('2d');
    canvas.width = 640;
    canvas.height = 480;

    // One frame in flight at a time, so a slow inference pass slows the feed
    // instead of letting messages pile up. The slot is released when the
    // server replies (onmessage calls frameSlotRelease), not on send: an
    // earlier version only cleared it on send failure, which meant the stream
    // sent exactly one frame and then silently stalled forever.
    let inFlight = false;
    let watchdogId = null;

    const release = () => {
        inFlight = false;
        if (watchdogId) {
            clearTimeout(watchdogId);
            watchdogId = null;
        }
    };

    intervalId = setInterval(() => {
        if (!ws || ws.readyState !== WebSocket.OPEN || inFlight) return;
        if (video.readyState < 2) return;

        inFlight = true;
        // Watchdog: if the server never answers (dropped reply, wedged
        // handler), free the slot anyway. Without this the stream can still
        // stall permanently, just via a different path.
        watchdogId = setTimeout(release, FRAME_TIMEOUT_MS);

        try {
            ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
            canvas.toBlob(
                (blob) => {
                    if (blob && ws && ws.readyState === WebSocket.OPEN) {
                        ws.send(blob);
                        // inFlight stays set until the server responds, or the
                        // watchdog fires.
                    } else {
                        release();
                    }
                },
                'image/jpeg',
                JPEG_QUALITY
            );
        } catch (err) {
            release();
            console.warn('Could not capture a frame:', err);
        }
    }, FRAME_INTERVAL_MS);

    frameSlotRelease = release;
    stopWatchdog = () => {
        if (watchdogId) clearTimeout(watchdogId);
        watchdogId = null;
    };
}

// --- Alert overlay -------------------------------------------------------

function showAlert(detections) {
    alertOverlay?.classList.add('visible');
    clearTimeout(alertTimeout);

    // Surface the scores so it is obvious the alert is real and not a
    // coincidence, which was hard to confirm from the outside before.
    if (alertDetail && detections) {
        const conf = (detections.confidence ?? 0).toFixed(2);
        const pct = Math.round((detections.rel_area ?? 0) * 100);
        alertDetail.textContent = `confidence ${conf} · phone covers ${pct}% of frame`;
    }

    // Keep the alert up while the phone is still visible. A live feed at 5 FPS
    // refreshes this timer on every alerting frame, so a sustained detection
    // holds the overlay indefinitely instead of blinking out for 2s and
    // looking like nothing was ever detected.
    alertTimeout = setTimeout(() => {
        // Only hide if the stream has genuinely gone quiet.
        if (!alertOverlay?.classList.contains('visible')) return;
        alertOverlay.classList.remove('visible');
    }, ALERT_LINGER_MS);
}

// --- Logout --------------------------------------------------------------

logoutBtn?.addEventListener('click', async () => {
    stopGuard();
    try {
        await fetch('/api/logout', { method: 'POST', credentials: 'same-origin' });
    } catch {
        /* Logging out locally is enough even if the request fails. */
    }
    window.location.replace('index.html');
});

// --- Stats (stub) -------------------------------------------------------
// TODO: implement once the session-stats data model is decided. There is no
// persistence layer yet, so there is nothing real to display here.
statsBtn?.addEventListener('click', () => {
    alert('Stats coming soon!');
});