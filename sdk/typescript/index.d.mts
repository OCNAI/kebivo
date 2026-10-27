export interface FeedbackOptions {
  tags?: string[];
  previous_answer?: string;
  context?: string;
  agent_id?: string;
  conversation_id?: string;
  source_event_id?: string;
  user_reference?: string;
  metadata?: Record<string, unknown>;
  idempotency_key?: string;
}

export interface CorrectionOptions extends FeedbackOptions {
  source_reference?: string;
}

export interface AcceptedFeedback {
  event_id: string;
  status: string;
  processing_status: string;
}

export class KebivoError extends Error {}
export class KebivoHTTPError extends KebivoError {
  readonly status_code: number;
  readonly code: string | null;
  readonly details: unknown;
  readonly request_id: string | null;
}
export class KebivoConnectionError extends KebivoError {}

export class Kebivo {
  constructor(url: string, token: string, options?: { timeout?: number });
  report_gap(question: string, options?: FeedbackOptions): Promise<AcceptedFeedback>;
  submit_correction(question: string, correction: string, options?: CorrectionOptions): Promise<AcceptedFeedback>;
}
