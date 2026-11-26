// The dispute statement form and how the chat goes with it (no React: unit-testable).

export interface DisputeForm {
  statement: string;
  contactMethod: string;
  badContact: string;
  contact: string;
  attachLogs: boolean;
  badStatement: string;
}

export const defaultDispute: DisputeForm = {
  statement: '',
  // 26 nov 2026 (Kilombino TG 15581): the chat is attached unless the robot unticks it
  attachLogs: true,
  contactMethod: '',
  contact: '',
  badContact: '',
  badStatement: '',
};

/** Up to this size the chat rides inside the statement (as every coordinator reads it); a
 * longer one goes apart in chat_log (Roblosats coordinators from 26 nov 2026). */
export const MAX_STATEMENT = 50_000;

/** The statement to send and, for a chat too long to ride inside it, the chat apart. */
export const statementWithChat = (
  statement: string,
  messages: object[],
  attach: boolean,
): { statement: string; chatLog?: string } => {
  if (!attach) return { statement };
  const payload = JSON.stringify({ statement, messages }, null, 2);
  if (payload.length <= MAX_STATEMENT) return { statement: payload };
  return { statement, chatLog: JSON.stringify(messages) };
};
