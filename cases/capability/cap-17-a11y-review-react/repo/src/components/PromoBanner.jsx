export function PromoBanner({ text, videoUrl }) {
  return (
    <section className="promo">
      <p>{text}</p>
      <video src={videoUrl} autoPlay loop />
    </section>
  );
}
