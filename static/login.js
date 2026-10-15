const form = document.getElementById('login-form');
const errorBanner = document.getElementById('login-error');
const errorText = document.getElementById('login-error-text');
const submitBtn = document.getElementById('submit-btn');
const googleBtn = document.getElementById('google-btn');
const googleHint = document.getElementById('google-hint');
const switchBtn = document.getElementById('switch-mode');
const switchPrompt = document.getElementById('switch-prompt');
const title = document.getElementById('auth-title');
const subtitle = document.getElementById('auth-sub');
const passwordHint = document.getElementById('password-hint');
const emailField = document.getElementById('email-field');
const confirmField = document.getElementById('confirm-field');
const usernameInput = document.getElementById('username');
const emailInput = document.getElementById('email');
const passwordInput = document.getElementById('password');
const confirmInput = document.getElementById('confirm');

const MIN_PASSWORD_LENGTH = 8;

// 'login' or 'register'. Kept in the URL so a refresh, or a shared link, keeps
// the user on the tab they chose.
let mode = new URLSearchParams(location.search).get('mode') === 'register'
    ? 'register'
    : 'login';

// Errors arriving as a query string come from the Google callback redirect.
const redirectError = new URLSearchParams(location.search).get('error');
if (redirectError) {
    showError(redirectError);
    history.replaceState(null, '', location.pathname);
}

function showError(message) {
    if (!errorBanner) return;
    errorText.textContent = message;
    errorBanner.classList.add('visible');
}

function clearError() {
    errorBanner?.classList.remove('visible');
}

function setMode(next) {
    mode = next;
    const registering = mode === 'register';

    title.textContent = registering ? 'Create account' : 'Sign in';
    subtitle.textContent = registering
        ? 'Pick a username to get started'
        : 'Focus mode for your workspace';
    submitBtn.textContent = registering ? 'Create account' : 'Sign in';

    switchPrompt.textContent = registering ? 'Already have an account?' : 'New here?';
    switchBtn.textContent = registering ? 'Sign in instead' : 'Create an account';

    emailField.hidden = !registering;
    confirmField.hidden = !registering;
    passwordHint.hidden = !registering || !passwordInput.value;
    passwordInput.autocomplete = registering ? 'new-password' : 'current-password';

    // Retype the password so a value typed for the other form is never carried
    // across, which is how confusing "wrong password" bugs start.
    passwordInput.value = '';
    confirmInput.value = '';

    clearError();

    const url = registering ? '?mode=register' : location.pathname;
    history.replaceState(null, '', url);

    usernameInput.focus();
}

switchBtn?.addEventListener('click', () => {
    setMode(mode === 'login' ? 'register' : 'login');
});

/**
 * Mirrors the server's strength hints. Advisory only: the server accepts any
 * password of sufficient length, and a client-side rule that blocks what the
 * server permits is just a support ticket.
 */
function updatePasswordHint() {
    if (mode !== 'register') {
        passwordHint.hidden = true;
        return;
    }

    const value = passwordInput.value;
    if (!value) {
        passwordHint.hidden = true;
        return;
    }

    const problems = [];
    if (value.length < MIN_PASSWORD_LENGTH) {
        problems.push(`at least ${MIN_PASSWORD_LENGTH} characters`);
    }
    if (value.length >= MIN_PASSWORD_LENGTH && /^\d+$/.test(value)) {
        problems.push('not only numbers');
    }

    passwordHint.hidden = problems.length === 0;
    passwordHint.textContent = problems.length ? `Weak: needs ${problems.join(', ')}.` : '';
}

/**
 * Reflect whether Google sign-in is usable, so the button never silently
 * fails. It is a real OAuth redirect, never a credential form.
 */
function setGoogleState(configured) {
    if (!googleBtn) return;

    googleBtn.disabled = !configured;
    googleHint.hidden = configured;

    if (!configured) {
        googleHint.textContent =
            'Google sign-in needs GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env';
    }
}

googleBtn?.addEventListener('click', () => {
    clearError();
    googleBtn.disabled = true;
    googleBtn.textContent = 'Redirecting to Google...';
    // A full navigation, so Google can set its own cookies.
    location.href = '/api/auth/google';
});

passwordInput?.addEventListener('input', updatePasswordHint);

// Skip the form when a session already exists, and learn whether Google
// sign-in is available.
(async function init() {
    setMode(mode);

    try {
        const [me, config] = await Promise.all([
            fetch('/api/me', { credentials: 'same-origin' }),
            fetch('/api/config', { credentials: 'same-origin' }),
        ]);

        if (me.ok) {
            location.replace('dashboard.html');
            return;
        }

        const data = await config.json().catch(() => ({}));
        setGoogleState(data.google_sign_in === true);
    } catch {
        // Server unreachable: leave the form usable and let submit report it.
        setGoogleState(false);
    }
})();

form?.addEventListener('submit', async (event) => {
    event.preventDefault();
    clearError();

    const username = usernameInput.value.trim();
    const password = passwordInput.value;

    if (!username || !password) {
        showError('Enter both a username and a password.');
        (!username ? usernameInput : passwordInput)?.focus();
        return;
    }

    if (mode === 'register') {
        if (password.length < MIN_PASSWORD_LENGTH) {
            showError(`Password must be at least ${MIN_PASSWORD_LENGTH} characters.`);
            passwordInput.focus();
            return;
        }
        if (confirmInput.value !== password) {
            showError('Passwords do not match.');
            confirmInput.focus();
            return;
        }
    }

    const body =
        mode === 'register'
            ? {
                  username,
                  password,
                  confirm: confirmInput.value,
                  email: emailInput.value.trim() || undefined,
              }
            : { username, password };

    const endpoint = mode === 'register' ? '/api/register' : '/api/login';
    const verb = mode === 'register' ? 'Creating account...' : 'Signing in...';

    submitBtn.disabled = true;
    submitBtn.textContent = verb;

    try {
        const res = await fetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify(body),
        });

        if (res.ok) {
            location.replace('dashboard.html');
            return;
        }

        const data = await res.json().catch(() => ({}));
        showError(data.detail || 'Something went wrong. Please try again.');

        if (res.status === 409) {
            // Username is taken: send them to the sign-in tab, keeping the name.
            setMode('login');
            usernameInput.value = username;
            showError(data.detail);
        } else {
            passwordInput.value = '';
            passwordInput.focus();
        }
    } catch {
        showError('Could not reach the server. Is it running?');
    } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = mode === 'register' ? 'Create account' : 'Sign in';
    }
});