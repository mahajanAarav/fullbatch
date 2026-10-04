import { Link, Route, Routes } from 'react-router-dom'
import Buy from './pages/Buy'
import Landing from './pages/Landing'
import OrderStatus from './pages/OrderStatus'
import Sell from './pages/Sell'

export default function App() {
  return (
    <>
      <header className="topbar">
        <Link to="/" className="brand">
          full<span>batch</span>
        </Link>
        <nav>
          <Link to="/sell">Sell</Link>
          <Link to="/buy">Buy</Link>
        </nav>
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
        Demo runs on the PayPal sandbox: no real money moves. Chat assistant powered by AI; it can make mistakes.
      </footer>
    </>
  )
}
