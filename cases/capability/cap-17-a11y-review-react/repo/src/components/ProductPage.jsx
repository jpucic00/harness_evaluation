import { ProductCard } from "./ProductCard";
import { PromoBanner } from "./PromoBanner";

export function ProductPage({ category, products, promo, onOpen }) {
  return (
    <main>
      <h1>{category.title}</h1>
      <PromoBanner text={promo.text} videoUrl={promo.videoUrl} />
      <h4>Bestsellers</h4>
      <div className="grid">
        {products.map((p) => (
          <ProductCard key={p.id} product={p} onOpen={onOpen} />
        ))}
      </div>
    </main>
  );
}
