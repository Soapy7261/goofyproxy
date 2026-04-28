export function decodeAuth(authString: string): { userId: string; password: string } | null {
    try {
        // Decode base64 to raw bytes
        const rawBytes = Buffer.from(authString, 'base64');

        if (rawBytes.length < 2) {
            return null;
        }

        let offset = 0;

        // Read first byte: number of bytes for userId UTF-8 representation
        const userIdLength = rawBytes[offset];
        offset += 1;

        if (offset + userIdLength > rawBytes.length) {
            return null;
        }

        // Read userId bytes and decode from UTF-8
        const userIdBytes = rawBytes.slice(offset, offset + userIdLength);
        const userId = userIdBytes.toString('utf-8');
        offset += userIdLength;

        // Read next byte: number of bytes for password UTF-8 representation
        if (offset >= rawBytes.length) {
            return null;
        }
        const passwordLength = rawBytes[offset];
        offset += 1;

        if (offset + passwordLength > rawBytes.length) {
            return null;
        }

        // Read password bytes and decode from UTF-8
        const passwordBytes = rawBytes.slice(offset, offset + passwordLength);
        const password = passwordBytes.toString('utf-8');

        return { userId, password };
    } catch (error) {
        return null;
    }
}
