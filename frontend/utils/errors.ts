/** An API failure with a user-presentable message (and Risk's individual reasons, if any). */
export class ApiError extends Error {
  constructor(
    message: string,
    public status = 0,
    public reasons: string[] = [],
  ) {
    super(message);
  }
}
