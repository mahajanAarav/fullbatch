// Grey placeholders shaped like the real thing, so the page does not jump when data arrives.
export function DropCardSkeleton() {
  return (
    <div className="card drop skeleton-card" aria-hidden="true">
      <div className="skeleton skeleton-tile" />
      <div className="skeleton skeleton-line" style={{ width: '60%' }} />
      <div className="skeleton skeleton-line" style={{ width: '40%' }} />
      <div className="skeleton skeleton-bar" />
      <div className="skeleton skeleton-line" style={{ width: '85%' }} />
      <div className="skeleton skeleton-button" />
    </div>
  )
}

export function DropGridSkeleton({ count = 3 }: { count?: number }) {
  return (
    <div className="grid" role="status" aria-label="Loading drops">
      {Array.from({ length: count }, (_, i) => (
        <DropCardSkeleton key={i} />
      ))}
    </div>
  )
}
