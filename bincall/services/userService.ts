import * as fs from 'fs/promises';
import * as path from 'path';
import { CONFIG } from '../config';
import { validateUserId, validatePassword } from '../utils/validation';
import { hashPassword, verifyPassword } from '../utils/crypto';

interface UserData {
    id: string;
    passwordHash: string;
    incomingCalls: IncomingCall[];
}

export interface IncomingCall {
    caller: string;
    timestamp: number;
    answered: boolean;
}

export class UserService {
    private usersDir: string;

    constructor() {
        this.usersDir = CONFIG.USERS_DIR;
    }

    private getUserDir(userId: string): string {
        return path.join(this.usersDir, userId);
    }

    private getUserFilePath(userId: string): string {
        return path.join(this.getUserDir(userId), 'user.json');
    }

    async userExists(userId: string): Promise<boolean> {
        try {
            await fs.access(this.getUserFilePath(userId));
            return true;
        } catch {
            return false;
        }
    }

    async getUser(userId: string): Promise<UserData | null> {
        try {
            const userData = await fs.readFile(this.getUserFilePath(userId), 'utf-8');
            return JSON.parse(userData) as UserData;
        } catch {
            return null;
        }
    }

    async createUser(userId: string, password: string): Promise<UserData | null> {
        if (!validateUserId(userId) || !validatePassword(password)) {
            return null;
        }

        if (await this.userExists(userId)) {
            return null
        }

        const userDir = this.getUserDir(userId);
        const userFilePath = this.getUserFilePath(userId);

        const userData: UserData = {
            id: userId,
            passwordHash: hashPassword(password),
            incomingCalls: [],
        };

        await fs.mkdir(userDir, { recursive: true });
        await fs.writeFile(userFilePath, JSON.stringify(userData, null, 2), 'utf-8');

        return userData;
    }

    async deleteUser(userId: string): Promise<boolean> {
        const userDir = this.getUserDir(userId);
        try {
            await fs.rm(userDir, { recursive: true, force: true });
            return true
        } catch (error) {
            console.error(`Error deleting directory "${userDir}": ${error}`);
            return false
        }
    }

    async verifyUser(userId: string, password: string): Promise<boolean> {
        const user = await this.getUser(userId);
        if (!user) {
            return false;
        }
        return verifyPassword(password, user.passwordHash);
    }

    async updateUser(user: UserData): Promise<void> {
        const userFilePath = this.getUserFilePath(user.id);
        await fs.writeFile(userFilePath, JSON.stringify(user, null, 2), 'utf-8');
    }

    async addIncomingCall(userId: string, caller: string, timestamp: number): Promise<boolean> {
        const user = await this.getUser(userId);
        if (!user) {
            return false;
        }

        user.incomingCalls.push({
            caller,
            timestamp,
            answered: false
        });

        await this.updateUser(user);
        return true;
    }

    async getIncomingCalls(userId: string): Promise<Array<IncomingCall>> {
        const user = await this.getUser(userId);
        if (!user) {
            return new Array<IncomingCall>();
        }

        const sixtySecondsAgo = Date.now() - 60000;
        user.incomingCalls = user.incomingCalls.filter(
            call => call.timestamp > sixtySecondsAgo
        );
        await this.updateUser(user);
        return user.incomingCalls;
    }

    async getIncomingCall(
        userId: string,
        callerId: string,
        timestamp: number | null,
        skip_answered: boolean,
        remove: boolean
    ): Promise<IncomingCall | null> {
        const user = await this.getUser(userId);
        if (!user) {
            return null;
        }

        const sixtySecondsAgo = Date.now() - 60000;
        user.incomingCalls = user.incomingCalls.filter(
            call => call.timestamp > sixtySecondsAgo
        );

        let foundCall: IncomingCall | null = null
        for (let i = 0; i < user.incomingCalls.length; i++) {
            const call = user.incomingCalls[i];
            if (call.caller !== callerId) {
                continue;
            }
            if (timestamp !== null && call.timestamp !== timestamp) {
                continue;
            }
            if (skip_answered && call.answered) {
                continue;
            }

            foundCall = call;
            if (remove) {
                user.incomingCalls.splice(i, 1)
                i--;
            }
        }

        await this.updateUser(user);
        return foundCall;
    }

    async cleanupOldCalls(userId: string): Promise<void> {
        const user = await this.getUser(userId);
        if (!user) {
            return;
        }

        const sixtySecondsAgo = Date.now() - 60000;
        user.incomingCalls = user.incomingCalls.filter(
            call => call.timestamp > sixtySecondsAgo
        );
        await this.updateUser(user);
    }

    async answerCall(userId: string, callerId: string, timestamp: number): Promise<boolean> {
        const user = await this.getUser(userId);
        if (!user) {
            return false;
        }

        const callIndex = user.incomingCalls.findIndex(
            call => call.caller === callerId && call.timestamp === timestamp
        );

        if (callIndex === -1) {
            return false;
        }

        user.incomingCalls[callIndex].answered = true;
        await this.updateUser(user);
        return true;
    }
}
