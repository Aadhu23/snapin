const toggleBtn = document.getElementById('toggle-btn');
const video = document.getElementById('webcam');
const alertOverlay = document.getElementById('alert-overlay');
const alertDetail = document.getElementById('alert-detail');
const statusDot = document.getElementById('status-dot');
const statusText = document.getElementById('status-text');
const stageEmpty = document.getElementById('stage-empty');
const logoutBtn = document.getElementById('logout-btn');
const identityBox = document.getElementById('identity');
const identityName = document.getElementById('identity-name');
const identityAvatar = document.getElementById('identity-avatar');
const identityInitials = document.getElementById('identity-initials');
const accountBar = document.getElementById('account-bar');
const accountUsername = document.getElementById('account-username');
const accountEmail = document.getElementById('account-email');
const accountMethods = document.getElementById('account-methods');
const accountGoogleLink = document.getElementById('account-google-link');

let isRunning = false;
let ws = null;
let stream = null;
let intervalId = null;
let alertTimeout = null;

// Set by startFrameStream so the WebSocket message handler can release the
// capture slot once the server has answered.
let frameSlotRelease = null;
let stopWatchdog = null;

// Whether the server has Google credentials configured. Decides if the
// "Link Google" affordance is worth showing.
let googleSignInAvailable = false;

// Bounds the frame send rate. Inference takes longer than this on CPU, and
// the client waits for each response, so this is a ceiling, not a floor.
const FRAME_INTERVAL_MS = 200;
const JPEG_QUALITY = 0.6;

// How long the alert overlay stays after the last alerting frame. Covers a
// few missed frames without feeling stuck once the phone is put away.
const ALERT_LINGER_MS = 3000;

// Upper bound on how long one frame may hold the capture slot, so a lost
// reply cannot stop the feed permanently.
const FRAME_TIMEOUT_MS = 5000;

// --- Session --------------------------------------------------------------

(async function init() {
    try {
        const [me, config] = await Promise.all([
            fetch('/api/me', { credentials: 'same-origin' }),
            fetch('/api/config', { credentials: 'same-origin' }),
        ]);

        if (!me.ok) {
            location.replace('index.html');
            return;
        }

        const cfg = await config.json().catch(() => ({}));
        googleSignInAvailable = cfg.google_sign_in === true;

        const { user } = await me.json();
        renderIdentity(user);
    } catch {
        location.replace('index.html');
    }
})();

function renderIdentity(user) {
    if (!user || !identityBox) return;

    const label = user.display_name || user.username || '';
    // A Google account arrives as google:<sub>; show the human part.
    const friendly = label.startsWith('google:') ? user.display_name || user.email : label;

    if (identityName) identityName.textContent = friendly || 'Signed in';
    identityBox.hidden = false;

    if (user.picture && identityAvatar) {
        identityAvatar.src = user.picture;
        identityAvatar.alt = '';
        identityAvatar.hidden = false;
    } else if (identityInitials) {
        const initials = (friendly || '?')
            .split(/\s+/)
            .filter(Boolean)
            .slice(0, 2)
            .map((part) => part[0].toUpperCase())
            .join('');
        identityInitials.textContent = initials || '?';
        identityInitials.hidden = false;
    }

    renderAccountBar(user, friendly);
}

/**
 * Show which sign-in methods this account has. Without it a user who signed up
 * with a password has no idea that Google sign-in is even possible, and a user
 * who linked Google cannot tell whether their password still works.
 */
function renderAccountBar(user, friendly) {
    if (!accountBar) return;

    accountUsername.textContent = friendly || user.username;
    accountEmail.textContent = user.email || '';
    accountEmail.hidden = !user.email;

    const methods = [];
    if (user.can_use_password) methods.push('Password');
    if (user.provider === 'google') methods.push('Google');

    accountMethods.textContent = methods.join(' · ');
    accountBar.hidden = false;

    // Offer linking only when it would actually do something.
    if (accountGoogleLink) {
        const needsLink = user.provider !== 'google' && googleSignInAvailable;
        accountGoogleLink.hidden = !needsLink;
    }
}

// --- Guard control --------------------------------------------------------

toggleBtn?.addEventListener('click', async () => {
    if (isRunning) {
        stopGuard();
    } else {
        await startGuard();
    }
});

async function startGuard() {
    try {
        stream = await navigator.mediaDevices.getUserMedia({
            video: { width: 640, height: 480 },
        });
        video.srcObject = stream;

        // Wait for real dimensions, otherwise the first drawImage samples a
        // 0x0 frame.
        await new Promise((resolve) => {
            if (video.videoWidth) return resolve();
            video.addEventListener('loadedmetadata', resolve, { once: true });
        });

        setStageLive(true);
        connectWebSocket();

        isRunning = true;
        toggleBtn.classList.add('active');
        toggleBtn.querySelector('span').textContent = 'Stop guard';
    } catch (err) {
        console.error('Could not start the guard:', err);
        showCameraError(err);
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
    if (toggleBtn) toggleBtn.querySelector('span').textContent = 'Start guard';
    setStatus('Disconnected', '');
    setStageLive(false);
    hideAlert();
}

function setStageLive(live) {
    stageEmpty?.classList.toggle('hidden', live);
    toggleBtn?.classList.toggle('active', live);
}

function showCameraError(err) {
    const denied = err?.name === 'NotAllowedError';
    setStatus(denied ? 'Camera blocked' : 'No camera', 'error');
    const title = document.getElementById('stage-empty-title');
    if (title) {
        title.textContent = denied ? 'Camera permission denied' : 'No camera available';
    }
}

function setStatus(text, state) {
    if (statusText) statusText.textContent = text;
    statusDot?.classList.remove('live', 'error');
    if (state) statusDot?.classList.add(state);
}

// --- WebSocket ------------------------------------------------------------

function connectWebSocket() {
    const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${protocol}://${location.host}/ws/detect`);

    ws.onopen = () => {
        setStatus('Live', 'live');
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
        } else if (data.detections?.reason) {
            // A miss carries a reason and a hint, so "it didn't alert" is
            // diagnosable from the console.
            console.debug(
                `No alert (${data.detections.reason}) ` +
                `conf=${data.detections.confidence} rel_area=${data.detections.rel_area}. ` +
                `${data.detections.hint || ''}`
            );
        }
    };

    ws.onerror = () => console.error('WebSocket error');

    ws.onclose = (event) => {
        // A rejected handshake never reaches onclose with a code: the server
        // refuses the upgrade at the HTTP layer, so the socket errors instead.
        const sessionLost = event.code === 4401 || event.code === 1006;
        setStatus(sessionLost ? 'Session expired' : 'Disconnected', sessionLost ? 'error' : '');

        if (isRunning) stopGuard();
        if (sessionLost) location.replace('index.html');
    };
}

// --- Frame streaming ------------------------------------------------------

function startFrameStream() {
    const canvas = document.createElement('canvas');
    const ctx = canvas.getContext('2d');
    canvas.width = 640;
    canvas.height = 480;

    // One frame in flight at a time, so a slow inference pass slows the feed
    // instead of letting messages pile up. The slot is released when the
    // server replies (onmessage calls frameSlotRelease), not on send: an
    // earlier version only cleared it on send failure, which meant the stream
    // sent exactly one frame and then stalled forever.
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
        // If the server never answers, free the slot anyway.
        watchdogId = setTimeout(release, FRAME_TIMEOUT_MS);

        try {
            ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
            canvas.toBlob(
                (blob) => {
                    if (blob && ws && ws.readyState === WebSocket.OPEN) {
                        ws.send(blob);
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

// --- Alert overlay --------------------------------------------------------

function showAlert(detections) {
    alertOverlay?.classList.add('visible');
    clearTimeout(alertTimeout);

    // Surface the scores, so an alert can be confirmed rather than assumed.
    if (alertDetail && detections) {
        const conf = (detections.confidence ?? 0).toFixed(2);
        const pct = Math.round((detections.rel_area ?? 0) * 100);
        alertDetail.textContent = `confidence ${conf} · ${pct}% of frame`;
    }

    // Refreshed on every alerting frame, so a sustained detection holds the
    // overlay up instead of blinking out.
    alertTimeout = setTimeout(hideAlert, ALERT_LINGER_MS);
}

function hideAlert() {
    alertOverlay?.classList.remove('visible');
}

// --- Sign out -------------------------------------------------------------

logoutBtn?.addEventListener('click', async () => {
    stopGuard();
    try {
        await fetch('/api/logout', { method: 'POST', credentials: 'same-origin' });
    } catch {
        // Signing out locally is enough even if the request fails.
    }
    location.replace('index.html');
});

// --- Stats ----------------------------------------------------------------
// The panel lives on its own page; the button is a plain link so it opens
// fast and works without JavaScript.