export function Avatar({ user }) {
  return (
    <div className="avatar">
      <img src={user.photoUrl} width={48} height={48} />
      <span>{user.name}</span>
    </div>
  );
}
