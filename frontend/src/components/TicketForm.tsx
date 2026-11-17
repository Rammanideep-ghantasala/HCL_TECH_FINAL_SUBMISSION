import type { FormEvent, RefObject } from "react";
import type { ExampleTicket, Limits } from "../api/types";
import { CATEGORY_FILTER_NOTE } from "../constants";
import { FIELD_LABELS, limitFor, type FieldErrors, type FieldName, type FormValues } from "../state/search";

export interface FieldRefs {
  subject: RefObject<HTMLInputElement | null>;
  description: RefObject<HTMLTextAreaElement | null>;
  product: RefObject<HTMLInputElement | null>;
  error_detail: RefObject<HTMLInputElement | null>;
  category: RefObject<HTMLSelectElement | null>;
}

function ids(name: FieldName) {
  const id = `field-${name}`;
  return { id, errorId: `${id}-error`, countId: `${id}-count`, hintId: `${id}-hint` };
}

export function describedBy(
  ...parts: Array<string | false | null | undefined>
): string | undefined {
  const present = parts.filter((part): part is string => typeof part === "string");
  return present.length > 0 ? present.join(" ") : undefined;
}

export function CharCount({ id, length, max }: { id: string; length: number; max: number }) {
  const over = length > max;
  return (
    <p id={id} className={over ? "char-count char-count-over" : "char-count"}>
      {length} / {max} characters
      {over ? ` — ${length - max} over the limit` : ""}
    </p>
  );
}

export function FieldError({ id, message }: { id: string; message: string | undefined }) {
  if (!message) {
    return null;
  }
  return (
    <p id={id} className="field-error">
      <span className="field-error-tag">Correct this:</span> {message}
    </p>
  );
}

export interface TicketFormProps {
  values: FormValues;
  errors: FieldErrors;
  limits: Limits;
  categories: string[];
  examples: ExampleTicket[];
  /** GET /api/meta failed: built-in limits are in use and there are no categories. */
  metaFailed: boolean;
  /** The server reported that no search method is available. */
  searchUnavailable: boolean;
  /** The form matches a search that is still in flight, so Search would be ignored. */
  identicalInFlight: boolean;
  subjectRef: FieldRefs["subject"];
  descriptionRef: FieldRefs["description"];
  productRef: FieldRefs["product"];
  errorDetailRef: FieldRefs["error_detail"];
  categoryRef: FieldRefs["category"];
  searchButtonRef: RefObject<HTMLButtonElement | null>;
  onChange: (field: FieldName, value: string) => void;
  onTopKChange: (count: 3 | 5) => void;
  onSubmit: () => void;
  onClear: () => void;
  onLoadExample: (example: ExampleTicket) => void;
}

/** The ticket-entry form. It only edits values; searching is decided by the parent. */
export function TicketForm({
  values,
  errors,
  limits,
  categories,
  examples,
  metaFailed,
  searchUnavailable,
  identicalInFlight,
  subjectRef,
  descriptionRef,
  productRef,
  errorDetailRef,
  categoryRef,
  searchButtonRef,
  onChange,
  onTopKChange,
  onSubmit,
  onClear,
  onLoadExample,
}: TicketFormProps) {
  const subject = ids("subject");
  const description = ids("description");
  const product = ids("product");
  const errorDetail = ids("error_detail");
  const category = ids("category");

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    onSubmit();
  };

  // A remembered category that the server no longer lists is still shown, so the select stays truthful.
  const categoryOptions =
    values.category !== "" && !categories.includes(values.category)
      ? [...categories, values.category]
      : categories;

  const subjectMax = limitFor("subject", limits);
  const descriptionMax = limitFor("description", limits);
  const productMax = limitFor("product", limits);
  const errorDetailMax = limitFor("error_detail", limits);

  return (
    <section className="panel entry" aria-labelledby="entry-heading">
      <h2 id="entry-heading" className="panel-heading">
        New ticket
      </h2>

      {metaFailed ? (
        <p className="inline-warning">
          <span className="inline-warning-tag">Note:</span> Settings could not be loaded from the
          server. Built-in length limits are in use and the category filter has no options. You can
          still search.
        </p>
      ) : null}
      {searchUnavailable ? (
        <p className="inline-warning">
          <span className="inline-warning-tag">Note:</span> The server reports that search is not
          available right now. A search may fail until it recovers.
        </p>
      ) : null}

      {examples.length > 0 ? (
        <div className="examples" role="group" aria-labelledby="examples-label">
          <p id="examples-label" className="examples-label">
            Load an example <span className="basis">(fills the form; it does not search)</span>
          </p>
          <ul className="example-list">
            {examples.map((example, index) => (
              <li key={`${index}-${example.ticket_id}`}>
                <button
                  type="button"
                  className="button button-small example-button"
                  onClick={() => onLoadExample(example)}
                >
                  <span className="example-number">Example {index + 1}</span>
                  {example.subject.trim() !== "" ? (
                    <span className="example-subject">{example.subject}</span>
                  ) : null}
                </button>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <form className="ticket-form" onSubmit={handleSubmit} noValidate>
        <div className="field">
          <label htmlFor={subject.id}>{FIELD_LABELS.subject}</label>
          <input
            id={subject.id}
            ref={subjectRef}
            name="subject"
            type="text"
            autoComplete="off"
            value={values.subject}
            onChange={(event) => onChange("subject", event.target.value)}
            aria-invalid={errors.subject ? true : undefined}
            aria-describedby={describedBy(
              subjectMax !== null && subject.countId,
              errors.subject && subject.errorId,
            )}
          />
          {subjectMax !== null ? (
            <CharCount id={subject.countId} length={values.subject.length} max={subjectMax} />
          ) : null}
          <FieldError id={subject.errorId} message={errors.subject} />
        </div>

        <div className="field">
          <label htmlFor={description.id}>{FIELD_LABELS.description}</label>
          <textarea
            id={description.id}
            ref={descriptionRef}
            name="description"
            rows={7}
            autoComplete="off"
            value={values.description}
            onChange={(event) => onChange("description", event.target.value)}
            aria-invalid={errors.description ? true : undefined}
            aria-describedby={describedBy(
              description.hintId,
              descriptionMax !== null && description.countId,
              errors.description && description.errorId,
            )}
          />
          <p id={description.hintId} className="hint">
            What the customer reported, in their words or yours. A subject or a description is
            required.
          </p>
          {descriptionMax !== null ? (
            <CharCount
              id={description.countId}
              length={values.description.length}
              max={descriptionMax}
            />
          ) : null}
          <FieldError id={description.errorId} message={errors.description} />
        </div>

        <fieldset className="fieldset">
          <legend>Add detail if you know it (optional)</legend>

          <div className="field">
            <label htmlFor={product.id}>{FIELD_LABELS.product}</label>
            <input
              id={product.id}
              ref={productRef}
              name="product"
              type="text"
              autoComplete="off"
              value={values.product}
              onChange={(event) => onChange("product", event.target.value)}
              aria-invalid={errors.product ? true : undefined}
              aria-describedby={describedBy(
                productMax !== null && product.countId,
                errors.product && product.errorId,
              )}
            />
            {productMax !== null ? (
              <CharCount id={product.countId} length={values.product.length} max={productMax} />
            ) : null}
            <FieldError id={product.errorId} message={errors.product} />
          </div>

          <div className="field">
            <label htmlFor={errorDetail.id}>{FIELD_LABELS.error_detail}</label>
            <input
              id={errorDetail.id}
              ref={errorDetailRef}
              name="error_detail"
              type="text"
              autoComplete="off"
              value={values.error_detail}
              onChange={(event) => onChange("error_detail", event.target.value)}
              aria-invalid={errors.error_detail ? true : undefined}
              aria-describedby={describedBy(
                errorDetailMax !== null && errorDetail.countId,
                errors.error_detail && errorDetail.errorId,
              )}
            />
            {errorDetailMax !== null ? (
              <CharCount
                id={errorDetail.countId}
                length={values.error_detail.length}
                max={errorDetailMax}
              />
            ) : null}
            <FieldError id={errorDetail.errorId} message={errors.error_detail} />
          </div>
        </fieldset>

        <div className="field">
          <label htmlFor={category.id}>{FIELD_LABELS.category}</label>
          <div className="filter-row">
            <select
              id={category.id}
              ref={categoryRef}
              name="category"
              value={values.category}
              onChange={(event) => onChange("category", event.target.value)}
              aria-invalid={errors.category ? true : undefined}
              aria-describedby={describedBy(category.hintId, errors.category && category.errorId)}
            >
              <option value="">All categories</option>
              {categoryOptions.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="button button-small"
              disabled={values.category === ""}
              onClick={() => {
                onChange("category", "");
                // The button is about to become unavailable, so focus returns to the filter.
                categoryRef.current?.focus();
              }}
            >
              Clear filter
            </button>
          </div>
          <p id={category.hintId} className="hint">
            {CATEGORY_FILTER_NOTE}
          </p>
          <FieldError id={category.errorId} message={errors.category} />
        </div>

        <div className="field">
          <label htmlFor="result-count">Results to show</label>
          <select id="result-count" value={values.top_k ?? 3}
            onChange={(event) => onTopKChange(event.target.value === "5" ? 5 : 3)}
            aria-describedby="result-count-hint">
            <option value={3}>Top 3</option>
            <option value={5}>Top 5</option>
          </select>
          <p id="result-count-hint" className="hint">Maximum matching tickets. Fewer are shown when fewer qualify.</p>
        </div>

        <div className="form-actions">
          <button
            type="submit"
            ref={searchButtonRef}
            className="button button-primary"
            aria-disabled={identicalInFlight ? true : undefined}
          >
            Search
          </button>
          <button type="button" className="button" onClick={onClear}>
            Clear
          </button>
          {identicalInFlight ? (
            <span className="hint form-actions-note">This search is already running.</span>
          ) : null}
        </div>
      </form>
    </section>
  );
}
