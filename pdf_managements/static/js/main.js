const root = document.documentElement;
const body = document.body;
const mobileToggle = document.querySelector('[data-mobile-menu]');
const sidebar = document.querySelector('.sidebar');
const themeToggle = document.querySelector('[data-theme-toggle]');

function setTheme(theme) {
  body.classList.toggle('dark', theme === 'dark');
  localStorage.setItem('theme', theme);
}

if (mobileToggle) {
  mobileToggle.addEventListener('click', () => {
    sidebar.classList.toggle('open');
  });
}

document.querySelectorAll('.nav-link').forEach((link) => {
  link.addEventListener('click', () => {
    document.querySelectorAll('.nav-link').forEach((item) => item.classList.remove('active'));
    link.classList.add('active');
  });
});

const storedTheme = localStorage.getItem('theme');
if (storedTheme) {
  setTheme(storedTheme);
} else if (window.matchMedia('(prefers-color-scheme: dark)').matches) {
  setTheme('dark');
}

if (themeToggle) {
  themeToggle.addEventListener('click', () => {
    const nextTheme = body.classList.contains('dark') ? 'light' : 'dark';
    setTheme(nextTheme);
  });
}

window.addEventListener('click', (event) => {
  if (window.innerWidth < 1024 && sidebar.classList.contains('open') && !sidebar.contains(event.target) && !mobileToggle.contains(event.target)) {
    sidebar.classList.remove('open');
  }
});
