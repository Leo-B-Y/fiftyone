import { useSessionSetter } from "@fiftyone/state";
import { env } from "@fiftyone/utilities";
import { useCallback } from "react";
import { useSetRecoilState } from "recoil";
import { AppReadyState, EventHandlerHook } from "./registerEvent";
import { appReadyState, processState } from "./utils";

const useStateUpdate: EventHandlerHook = ({ router, readyStateRef }) => {
  const setter = useSessionSetter();
  const setReadyState = useSetRecoilState(appReadyState);

  return useCallback(
    (payload: any) => {
      console.log("useStateUpdate", payload.state);
      const state = processState(setter, payload.state);

      const searchParams = new URLSearchParams(router.history.location.search);

      if (payload.state.saved_view_slug) {
        searchParams.set(
          "view",
          encodeURIComponent(payload.state.saved_view_slug)
        );
      } else {
        searchParams.delete("view");
      }

      let search = searchParams.toString();
      if (search.length) {
        search = `?${search}`;
      }

      const path = payload.state.dataset
        ? `/datasets/${encodeURIComponent(payload.state.dataset)}${search}`
        : `/${search}`;

      if (readyStateRef.current !== AppReadyState.OPEN) {
        router.history.replace(path, state);
        router.load().then(() => setReadyState(AppReadyState.OPEN));
      } else {
        router.history.push(path, state);
      }
    },
    [readyStateRef, router, setter, setReadyState]
  );
};

export default useStateUpdate;
