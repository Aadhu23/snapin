const form = document.getElementById('link-form');
const errorBanner = document.getElementById('link-error');
const errorText = document.getElementById('link-error-text');
const submitBtn = document.getElementById('link-submit');
const skipBtn = document.getElementById('link-skip');
const emailEl = document.getElementById('link-email');
const usernameEl = document.getElementById('link-username');
const passwordInput = document.getElementById('link-password');

function showError(message) {
    errorText.textContent = message;
    errorBanner.classList.add('visible');
}

function clearError() {
    errorBanner.classList.remove('visible');
}

// The pending Google profile lives in an HttpOnly cookie, so the page asks the
// server who it is for rather than reading it from the client.
(async function init() {
    try {
        const res = await fetch('/api/me', { credentials: 'same-origin' });
        if (!res.ok) {
            location.replace('index.html');
            return;
        }

        const { user } = await res.json();
        usernameEl.textContent = user.username;
        emailEl.textContent = user.email || 'this address';
        passwordInput.focus();
    } catch {
        location.replace('index.html');
    }
})();

form?.addEventListener('submit', async (event) => {
    event.preventDefault();
    clearError();

    if (!passwordInput.value) {
        showError('Enter your current password.');
        passwordInput.focus();
        return;
    }

    submitBtn.disabled = true;
    submitBtn.textContent = 'Linking...';

    try {
        const res = await fetch('/api/link/google', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({ password: passwordInput.value }),
        });

        if (res.ok) {
            location.replace('dashboard.html');
            return;
        }

        const data = await res.json().catch(() => ({}));
        showError(data.detail || 'Could not link the account.');
        passwordInput.value = '';

        if (res.status === 400) {
            // The pending profile is gone; there is nothing to retry.
            setTimeout(() => location.replace('dashboard.html'), 2500);
        }
    } catch {
        showError('Could not reach the server. Is it running?');
    } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = 'Link Google account';
    }
});

skipBtn?.addEventListener('click', async () => {
    try {
        await fetch('/api/link/google/skip', {
            method: 'POST',
            credentials: 'same-origin',
        });
    } catch {
        /* Leaving anyway is fine if the cleanup request fails. */
    }
    location.replace('dashboard.html');
});