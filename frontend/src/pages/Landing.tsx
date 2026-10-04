import { Link } from 'react-router-dom'

export default function Landing() {
  return (
    <main className="landing">
      <section className="hero">
        <p className="eyebrow">Preorder drops for small sellers</p>
        <h1>Bake it only if enough people want it.</h1>
        <p className="lead">
          fullbatch runs limited preorder drops in a simple chat. Buyers’ payments are only <em>held</em> on PayPal. If the
          drop reaches its minimum by the deadline, everyone is charged. If not, every hold is released and nobody pays a
          cent.
        </p>
        <div className="choices">
          <Link to="/sell" className="choice card">
            <span className="choice-title">I’m selling</span>
            <span className="muted">Open a drop by chatting. Watch orders come in.</span>
            <span className="choice-go">Start a drop →</span>
          </Link>
          <Link to="/buy" className="choice card">
            <span className="choice-title">I’m buying</span>
            <span className="muted">Browse open drops and reserve yours in chat.</span>
            <span className="choice-go">See what’s open →</span>
          </Link>
        </div>
      </section>

      <section className="steps">
        <h2>How a drop works</h2>
        <ol>
          <li>
            <strong>Seller opens a drop</strong>
            <span>Item, price, quantity, a minimum, and a deadline. Just tell the assistant.</span>
          </li>
          <li>
            <strong>Buyers reserve and approve</strong>
            <span>PayPal places a hold on each payment. Nobody is charged yet.</span>
          </li>
          <li>
            <strong>Deadline: charge or release</strong>
            <span>Minimum met? Every hold is captured. Missed? Every hold is voided.</span>
          </li>
          <li>
            <strong>Plan the next one</strong>
            <span>The assistant reviews how it went and suggests the next drop.</span>
          </li>
        </ol>
      </section>
    </main>
  )
}
