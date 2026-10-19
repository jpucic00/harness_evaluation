import { SearchIcon } from "./icons";

export function SearchBar({ query, onChange, onSearch }) {
  return (
    <form role="search" onSubmit={onSearch}>
      <input
        type="search"
        placeholder="Search products"
        value={query}
        onChange={(e) => onChange(e.target.value)}
      />
      <button type="submit">
        <SearchIcon aria-hidden="true" />
      </button>
    </form>
  );
}
