const toggleBtn = document.getElementById('toggle-btn');
const video = document.getElementById('webcam');
const alertOverlay = document.getElementById('alert-overlay');
const connectionDot = document.getElementById('connection-dot');
const connectionText = document.getElementById('connection-text');

let isRunning = false;
let ws = null;
let stream = null;
let intervalId = null;

// Toggle Guard Logic
toggleBtn.addEventListener('click', async () => {
    if (!isRunning) {
        await startGuard();
    } else {
        stopGuard();
    }
});

async function startGuard() {
    try {
        // 1. Get Camera
        stream = await navigator.mediaDevices.getUserMedia({ video: { width: 640, height: 480 } });
        video.srcObject = stream;

        // 2. Connect WebSocket
        connectWebSocket();

        // 3. UI Updates
        isRunning = true;
        toggleBtn.classList.add('active');
        toggleBtn.querySelector('span').textContent = 'Guard Active';
    } catch (err) {
        console.error("Error starting:", err);
        alert("Could not access camera.");
    }
}

function stopGuard() {
    // 1. Stop sending frames
    if (intervalId) clearInterval(intervalId);

    // 2. Close WS
    if (ws) ws.close();

    // 3. Stop Camera
    if (stream) {
        stream.getTracks().forEach(track => track.stop());
        video.srcObject = null;
    }

    // 4. UI Updates
    isRunning = false;
    toggleBtn.classList.remove('active');
    toggleBtn.querySelector('span').textContent = 'Turn On Guard';
    connectionDot.classList.remove('active');
    connectionText.textContent = 'Disconnected';
    alertOverlay.classList.remove('visible');
}

function connectWebSocket() {
    // Adjust protocol for dev/prod
    const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const wsUrl = `${protocol}://${window.location.host}/ws/detect`;

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
        console.log("Connected to Server");
        connectionDot.classList.add('active');
        connectionText.textContent = 'Live Monitoring';

        // Start sending frames
        startFrameStream();
    };

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.alert) {
            showAlert();
        }
    };

    ws.onclose = () => {
        console.log("Disconnected");
        connectionDot.classList.remove('active');
        connectionText.textContent = 'Disconnected';
        if (isRunning) stopGuard(); // Auto-stop if connection lost
    };
}

function startFrameStream() {
    const canvas = document.createElement('canvas');
    const ctx = canvas.getContext('2d');
    canvas.width = 640;
    canvas.height = 480;

    // Send frame every 200ms (5 FPS)
    intervalId = setInterval(() => {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
            canvas.toBlob((blob) => {
                if (blob) ws.send(blob);
            }, 'image/jpeg', 0.6); // Compress to 60% quality
        }
    }, 200);
}

let alertTimeout;
function showAlert() {
    alertOverlay.classList.add('visible');
    clearTimeout(alertTimeout);

    // Auto-hide after 2s if no new alerts come
    alertTimeout = setTimeout(() => {
        alertOverlay.classList.remove('visible');
    }, 2000);
}
