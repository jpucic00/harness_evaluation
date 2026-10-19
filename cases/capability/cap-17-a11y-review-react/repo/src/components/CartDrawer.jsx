export function CartDrawer({ open, items, onClose, onCheckout }) {
  return (
    <aside className="drawer" aria-hidden={!open}>
      <h2>Your cart</h2>
      <ul>
        {items.map((item) => (
          <li key={item.id}>
            {item.name} &times; {item.qty}
          </li>
        ))}
      </ul>
      <button onClick={onCheckout} tabIndex={1}>
        Checkout
      </button>
      <button onClick={onClose} aria-label="Close cart">
        &times;
      </button>
    </aside>
  );
}
