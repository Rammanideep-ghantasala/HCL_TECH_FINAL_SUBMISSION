import { PRODUCT_NAME } from "../constants";

/** Product name and the data notice supplied by the server. */
export function Header({ dataNotice }: { dataNotice: string }) {
  return (
    <header className="app-header">
      <div className="app-header-inner">
        <div>
          <h1 className="app-title">{PRODUCT_NAME}</h1>
          <p className="app-tagline">
            Find similar past tickets and judge whether their resolution applies. You investigate
            and resolve the ticket.
          </p>
        </div>
        <p className="data-notice">
          <span className="data-notice-tag">Demo notice</span>
          <span>{dataNotice}</span>
        </p>
      </div>
    </header>
  );
}
