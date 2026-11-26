import { useContext, useEffect, useState } from 'react';
import { type Order } from '../../../models';
import {
  FederationContext,
  type UseFederationStoreType,
} from '../../../contexts/FederationContext';
import { GarageContext, type UseGarageStoreType } from '../../../contexts/GarageContext';
import { apiClient } from '../../../services/api';
import { decryptMessage } from '../../../pgp';
import { type EncryptedChatMessage, type ServerMessage } from '../EncryptedChat';

/** The order's chat, fetched from the coordinator and decrypted with this robot's key: what a
 * dispute statement attaches (moved out of DisputePrompt on 26 nov 2026, also used to attach
 * the chat after the statement). */
export const useDisputeChat = (
  order: Order,
  shortAlias: string,
): { messages: EncryptedChatMessage[]; loading: boolean } => {
  const { federation } = useContext<UseFederationStoreType>(FederationContext);
  const { garage } = useContext<UseGarageStoreType>(GarageContext);
  const [messages, setMessages] = useState<EncryptedChatMessage[]>([]);
  const [loading, setLoading] = useState<boolean>(true);

  const decryptMessages = async (
    serverMessages: ServerMessage[],
    peerPubKey: string,
  ): Promise<void> => {
    const slot = garage.getSlot();
    const robot = slot?.getRobot();
    if (slot && robot) {
      for (const dataFromServer of serverMessages) {
        if (dataFromServer.message.substring(0, 27) === `-----BEGIN PGP MESSAGE-----`) {
          const senderPubKey = dataFromServer.nick === slot.nickname ? robot.pubKey : peerPubKey;
          if (!senderPubKey || !robot.encPrivKey || !slot.token) {
            console.warn('[useDisputeChat] skipping decrypt: missing keys or token');
            continue;
          }
          const decryptedData = await decryptMessage(
            dataFromServer.message.split('\\').join('\n'),
            senderPubKey,
            robot.encPrivKey,
            slot.token,
          );
          setMessages((prev: EncryptedChatMessage[]) => {
            const x: EncryptedChatMessage = {
              index: dataFromServer.index,
              encryptedMessage: dataFromServer.message.split('\\').join('\n'),
              plainTextMessage: decryptedData.decryptedMessage,
              validSignature: decryptedData.validSignature,
              userNick: dataFromServer.nick,
              time: dataFromServer.time,
            };
            return [...prev.filter((m) => m.index !== x.index), x].sort(
              (a, b) => a.index - b.index,
            );
          });
        }
      }
    }
    setLoading(false);
  };

  useEffect(() => {
    const url = federation.getCoordinator(shortAlias)?.url ?? '';
    if (!url) {
      setLoading(false);
      return;
    }
    apiClient
      .get(url, `/api/chat/?order_id=${order.id}&offset=0`, {
        tokenSHA256: garage.getSlot()?.getRobot()?.tokenSHA256 ?? '',
      })
      .then((results: object | undefined) => {
        if (results != null) {
          const r = results as { messages: ServerMessage[]; peer_pubkey?: string };
          void decryptMessages(r.messages ?? [], (r.peer_pubkey ?? '').split('\\').join('\n'));
        } else {
          setLoading(false);
        }
      })
      .catch(() => {
        setLoading(false);
      });
  }, []);

  return { messages, loading };
};
