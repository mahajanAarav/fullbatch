import { useState } from 'react'
import { Link, NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { myOrders } from './api'
import { AuthProvider, RequireAuth } from './auth'
import { VerifyEmailDialog } from './components/VerifyEmailDialog'
import { useAuth } from './authContext'
import { usePolling } from './hooks'
import Home from './pages/Home'
import OrderStatus from './pages/OrderStatus'
import Orders from './pages/Orders'
import Sell from './pages/Sell'
import Shop from './pages/Shop'
import SignIn from './pages/SignIn'

function Nav() {
  const { me, signOut } = useAuth()
  const user = me?.user ?? null
  // A small badge on "My orders" while something is waiting on the buyer.
  const { data: orders } = usePolling(myOrders, 15_000, user !== null)
  const waiting = orders?.filter((o) => o.status === 'reserved').length ?? 0

  return (
    <nav aria-label="Main">
      <NavLink to="/" end>
        Browse
      </NavLink>
      {user && (
        <NavLink to="/orders">
          My orders
          {waiting > 0 && <span className="nav-badge" aria-label={`${waiting} waiting for approval`}>{waiting}</span>}
        </NavLink>
      )}
      <NavLink to="/sell">Sell</NavLink>
      {me !== null &&
        (user ? (
          <div className="account">
            <span className="account-name" title={user.email}>
              {user.name}
            </span>
            <button className="link-button small" onClick={signOut}>
              Sign out
            </button>
          </div>
        ) : (
          <Link to="/signin" className="button button-small">
            Sign in
          </Link>
        ))}
    </nav>
  )
}

// A slim reminder under the header while someone's email is unconfirmed.
function EmailBanner() {
  const { me } = useAuth()
  const [open, setOpen] = useState(false)
  if (!me?.user || me.user.email_verified) return null
  return (
    <div className="email-banner" role="status">
      <div className="wrap email-banner-in">
        <span>Verify your email to reserve drops or open a shop.</span>
        <button className="link-button" onClick={() => setOpen(true)}>
          Verify now
        </button>
      </div>
      <VerifyEmailDialog open={open} onClose={() => setOpen(false)} />
    </div>
  )
}

export default function App() {
  return (
    <AuthProvider>
      <header className="topbar">
        <div className="wrap topbar-in">
          <Link to="/" className="brand">
            full<span>batch</span>
          </Link>
          <Nav />
        </div>
      </header>
      <EmailBanner />
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/buy" element={<Navigate to="/" replace />} />
        <Route path="/shop/:id" element={<Shop />} />
        <Route path="/signin" element={<SignIn />} />
        <Route path="/sell" element={<RequireAuth><Sell /></RequireAuth>} />
        <Route path="/orders" element={<RequireAuth><Orders /></RequireAuth>} />
        <Route path="/orders/:id" element={<RequireAuth><OrderStatus /></RequireAuth>} />
        <Route path="*" element={<Home />} />
      </Routes>
      <footer className="foot">
        <div className="wrap">Demo on the PayPal sandbox. No real money moves. The assistant is AI and can make mistakes.</div>
      </footer>
    </AuthProvider>
  )
}
