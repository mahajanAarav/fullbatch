import { Link, NavLink, Route, Routes } from 'react-router-dom'
import { AuthProvider, RequireAuth } from './auth'
import { useAuth } from './authContext'
import Buy from './pages/Buy'
import Landing from './pages/Landing'
import OrderStatus from './pages/OrderStatus'
import Sell from './pages/Sell'
import SignIn from './pages/SignIn'

function AccountMenu() {
  const { me, signOut } = useAuth()
  if (me === null) return null
  if (!me.user) {
    return (
      <Link to="/signin" className="button button-small">
        Sign in
      </Link>
    )
  }
  return (
    <div className="account">
      <span className="account-name" title={me.user.email}>
        {me.user.name}
      </span>
      <button className="link-button small" onClick={signOut}>
        Sign out
      </button>
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
          <nav>
            <NavLink to="/buy">Browse drops</NavLink>
            <NavLink to="/sell">Sell</NavLink>
            <AccountMenu />
          </nav>
        </div>
      </header>
      <Routes>
        <Route path="/" element={<Landing />} />
        <Route path="/signin" element={<SignIn />} />
        <Route path="/sell" element={<RequireAuth><Sell /></RequireAuth>} />
        <Route path="/buy" element={<Buy />} />
        <Route path="/orders" element={<RequireAuth><OrderStatus /></RequireAuth>} />
        <Route path="/orders/:id" element={<RequireAuth><OrderStatus /></RequireAuth>} />
        <Route path="*" element={<Landing />} />
      </Routes>
      <footer className="foot">
        <div className="wrap">Demo on the PayPal sandbox. No real money moves. The assistant is AI and can make mistakes.</div>
      </footer>
    </AuthProvider>
  )
}
