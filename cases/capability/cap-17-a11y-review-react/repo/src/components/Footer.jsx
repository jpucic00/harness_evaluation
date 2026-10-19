export function Footer() {
  return (
    <footer>
      <img src="/img/divider.svg" alt="" />
      <nav aria-label="Footer">
        <a href="/help">Help centre</a>
        <a href="/returns">Returns and refunds</a>
        <a href="https://social.example/storefront" aria-label="Storefront on Social">
          <svg aria-hidden="true" focusable="false" width="16" height="16" />
        </a>
      </nav>
      <p className="muted">© 2026 Storefront</p>
    </footer>
  );
}
