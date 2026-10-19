export function ProductCard({ product, onOpen }) {
  return (
    <article className="card">
      <div className="card-media" onClick={() => onOpen(product.id)}>
        <img src={product.imageUrl} alt={product.name} />
      </div>
      <h3>{product.name}</h3>
      <p className="muted">{product.shortDescription}</p>
      <p className="price">{product.price}</p>
      <a href={`/products/${product.id}`}>Click here</a>
    </article>
  );
}
