import { NavLink, Link, Route, Routes } from 'react-router-dom'
import Buy from './pages/Buy'
import Landing from './pages/Landing'
import OrderStatus from './pages/OrderStatus'
import Sell from './pages/Sell'

export default function App() {
  return (
    <>
      <header className="topbar">
        <div className="wrap topbar-in">
          <Link to="/" className="brand">
            full<span>batch</span>
          </Link>
          <nav>
            <NavLink to="/buy">Browse drops</NavLink>
            <NavLink to="/sell">Sell</NavLink>
          </nav>
        </div>
      </header>
      <Routes>
        <Route path="/" element={<Landing />} />
        <Route path="/sell" element={<Sell />} />
        <Route path="/buy" element={<Buy />} />
        <Route path="/orders" element={<OrderStatus />} />
        <Route path="/orders/:id" element={<OrderStatus />} />
        <Route path="*" element={<Landing />} />
      </Routes>
      <footer className="foot">
        <div className="wrap">Demo on the PayPal sandbox. No real money moves. The assistant is AI and can make mistakes.</div>
      </footer>
    </>
  )
}
