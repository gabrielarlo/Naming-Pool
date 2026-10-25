function namingPool() {
  return {
    state: {
      joined: false,
      stage: "",
      is_moderator: false,
      total_suggestions: 0,
      voted_guests: 0,
      online_guests: 0,
      min_suggestions: 0,
      min_suggestions_met: false,
      suggestion_limit: 5,
      own_suggestion_count: 0,
      own_suggestions: [],
      can_submit: false,
      candidates: [],
      my_votes: [],
      vote_allowance: null,
      remaining_votes: null,
      results: [],
      winner: null,
      can_advance: false,
    },
    joinCode: "",
    moderatorCode: "",
    newName: "",
    newReason: "",
    draftVotes: [],
    dirtyVotes: false,
    busy: false,
    advanceDialogOpen: false,
    advanceDialogStage: "",
    advanceDialogTitle: "",
    advanceDialogMessage: "",
    error: "",
    notice: "",
    connection: "online",
    lastStage: null,
    deviceId: "",
    pollTimer: null,
    heartbeat: false,
    _noticeTimer: null,
    _heartbeatTimer: null,

    async init() {
      this.deviceId = this.ensureDeviceId();
      await this.refresh();
      this.pollTimer = window.setInterval(() => {
        if (!document.hidden) this.refresh();
      }, 3000);
      document.addEventListener("visibilitychange", () => {
        if (!document.hidden) this.refresh();
      });
    },

    ensureDeviceId() {
      const key = "naming-pool-device";
      let value = null;
      try {
        value = window.localStorage.getItem(key);
      } catch (_) {
        value = null;
      }
      if (!value) {
        if (window.crypto && typeof window.crypto.randomUUID === "function") {
          value = window.crypto.randomUUID();
        } else if (window.crypto && window.crypto.getRandomValues) {
          value = Array.from(window.crypto.getRandomValues(new Uint8Array(16)))
            .map((b) => b.toString(16).padStart(2, "0"))
            .join("");
        } else {
          value = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
        }
        try {
          window.localStorage.setItem(key, value);
        } catch (_) {
          // A blocked localStorage still leaves a per-page identity for this visit.
        }
      }
      return value;
    },

    headers(hasBody) {
      const headers = { "X-Device-Id": this.deviceId };
      if (hasBody) headers["Content-Type"] = "application/json";
      return headers;
    },

    async request(path, options = {}) {
      const hasBody = options.body !== undefined;
      try {
        const response = await fetch(path, {
          credentials: "same-origin",
          ...options,
          headers: { ...this.headers(hasBody), ...(options.headers || {}) },
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          this.connection = "online";
          this.setError(data.detail || "Something went wrong.");
          return null;
        }
        this.connection = "online";
        this.clearError();
        return data;
      } catch (_) {
        this.connection = "offline";
        this.setError("Connection lost. Retrying when the server is reachable.");
        return null;
      }
    },

    applyState(data, action = false) {
      const previousStage = this.state.stage;
      const previousVotes = this.state.my_votes || [];
      this.state = { ...this.state, ...data };
      if (
        !data.joined ||
        !this.state.is_moderator ||
        this.state.stage === "closed"
      ) {
        this.closeAdvanceDialog();
      }
      if (data.joined) this.beat();
      if (!data.joined) {
        this.draftVotes = [];
        this.dirtyVotes = false;
        this.lastStage = null;
        return;
      }
      const serverVotes = JSON.stringify(data.my_votes || []);
      if (action || this.lastStage === null || previousStage !== data.stage) {
        this.draftVotes = [...(data.my_votes || [])];
        this.dirtyVotes = false;
      } else if (
        !this.dirtyVotes &&
        serverVotes !== JSON.stringify(this.draftVotes)
      ) {
        this.draftVotes = [...(data.my_votes || [])];
      }
      if (previousStage && data.stage !== previousStage) {
        this.flash(this.stageNotice(data.stage));
      } else if (
        previousStage === "tiebreak" &&
        data.stage === "tiebreak" &&
        previousVotes.length > 0 &&
        (data.my_votes || []).length === 0
      ) {
        this.flash("The tiebreak is still tied. Your ballot was cleared; vote again.");
      }
      this.lastStage = data.stage;
    },

    async refresh() {
      if (this.busy) return;
      const data = await this.request("/api/state");
      if (data) this.applyState(data);
    },

    beat() {
      this.heartbeat = false;
      window.requestAnimationFrame(() => {
        this.heartbeat = true;
        window.clearTimeout(this._heartbeatTimer);
        this._heartbeatTimer = window.setTimeout(() => {
          this.heartbeat = false;
        }, 700);
      });
    },

    flash(message) {
      this.notice = message;
      window.clearTimeout(this._noticeTimer);
      this._noticeTimer = window.setTimeout(() => {
        this.notice = "";
      }, 5000);
    },

    setError(message) {
      this.error = message;
    },

    clearError() {
      this.error = "";
    },

    async join() {
      if (this.busy || !this.joinCode.trim()) return;
      this.busy = true;
      this.clearError();
      const data = await this.request("/api/join", {
        method: "POST",
        body: JSON.stringify({ code: this.joinCode }),
      });
      this.busy = false;
      if (data) {
        this.applyState(data, true);
        this.joinCode = "";
        this.flash("You are in the room.");
      }
    },

    async unlockModerator() {
      if (this.busy || !this.moderatorCode.trim()) return;
      this.busy = true;
      this.clearError();
      const data = await this.request("/api/moderator", {
        method: "POST",
        body: JSON.stringify({ code: this.moderatorCode }),
      });
      this.busy = false;
      if (data) {
        this.applyState(data, true);
        this.moderatorCode = "";
        this.flash("Moderator controls unlocked.");
      }
    },

    async submitSuggestion() {
      if (this.busy) return;
      const name = this.newName.trim();
      const reason = this.newReason.trim();
      if (!name || !reason) {
        this.setError("A name and a reason are both required.");
        return;
      }
      this.busy = true;
      this.clearError();
      const data = await this.request("/api/suggestions", {
        method: "POST",
        body: JSON.stringify({ name, reason }),
      });
      this.busy = false;
      if (data) {
        this.applyState(data, true);
        this.newName = "";
        this.newReason = "";
        this.flash("Name added to the pool.");
      }
    },

    toggleVote(id) {
      this.clearError();
      const index = this.draftVotes.indexOf(id);
      if (index !== -1) {
        this.draftVotes.splice(index, 1);
        this.dirtyVotes = true;
        return;
      }
      if (this.draftVotes.length >= this.state.vote_allowance) {
        this.setError(
          `You can select at most ${this.state.vote_allowance} name${
            this.state.vote_allowance === 1 ? "" : "s"
          } in this stage.`,
        );
        return;
      }
      this.draftVotes.push(id);
      this.dirtyVotes = true;
    },

    clearVotes() {
      this.draftVotes = [];
      this.dirtyVotes = true;
      this.clearError();
    },

    async saveVotes() {
      if (this.busy) return;
      this.busy = true;
      this.clearError();
      const data = await this.request("/api/votes", {
        method: "PUT",
        body: JSON.stringify({
          stage: this.state.stage,
          suggestion_ids: this.draftVotes,
        }),
      });
      this.busy = false;
      if (data) {
        this.applyState(data, true);
        this.flash("Votes saved.");
      }
    },

    openAdvanceDialog() {
      if (this.busy || !this.state.can_advance || this.advanceDialogOpen) return;
      this.advanceDialogStage = this.state.stage;
      this.advanceDialogTitle = this.advanceLabel;
      this.advanceDialogMessage = this.advanceConsequence(
        this.advanceDialogStage,
      );
      this.$refs.advanceDialog.showModal();
      this.advanceDialogOpen = true;
    },

    closeAdvanceDialog() {
      if (this.$refs.advanceDialog?.open) {
        this.$refs.advanceDialog.close();
      }
      this.advanceDialogOpen = false;
    },

    advanceConsequence(stage) {
      const messages = {
        submission: "Voting will open. This step does not publish results.",
        stage1: "Stage 1 results will be published and this cannot be undone.",
        stage2: "Stage 2 results will be published and this cannot be undone.",
        final:
          "The winner will be published and the event will close. If the final is tied, a tiebreak will open.",
        tiebreak:
          "The winner will be published. If the tiebreak is still tied, the votes reset so everyone can vote again.",
      };
      return messages[stage] || "";
    },

    async confirmAdvance() {
      if (this.busy) return;
      const stage = this.advanceDialogStage;
      if (!stage) return;
      this.closeAdvanceDialog();
      this.busy = true;
      this.clearError();
      const data = await this.request("/api/moderator/advance", {
        method: "POST",
        body: JSON.stringify({ stage }),
      });
      this.busy = false;
      if (data) {
        this.applyState(data, true);
        this.flash(data.notice || "Stage updated.");
      }
    },

    stageNotice(stage) {
      const messages = {
        stage1: "Stage 1 is open: names only, 10 votes each.",
        stage2: "Stage 2 is open: top 10 names with reasons, 3 votes each.",
        final: "The final is open: top 3 names, 1 vote each.",
        tiebreak: "The final is tied. The finalists are up for a re-vote.",
        closed: "The event is closed. The winner has been chosen.",
      };
      return messages[stage] || "The event has moved to a new stage.";
    },

    get stageLabel() {
      const labels = {
        submission: "Submissions",
        stage1: "Stage 1",
        stage2: "Stage 2",
        final: "Final",
        tiebreak: "Tiebreak",
        closed: "Closed",
      };
      return labels[this.state.stage] || "";
    },

    get stageEyebrow() {
      const labels = {
        submission: "Suggestions",
        stage1: "Voting round 1 of 3",
        stage2: "Voting round 2 of 3",
        final: "Final round",
        tiebreak: "Tiebreak",
        closed: "Results",
      };
      return labels[this.state.stage] || "";
    },

    get stageHeading() {
      const labels = {
        submission: "Add a name and its story",
        stage1: "Vote for the names you like",
        stage2: "Narrow the top 10 down to 3",
        final: "Choose the winner",
        tiebreak: "Break the tie",
        closed: this.state.winner
          ? `Winner: ${this.state.winner.name}`
          : "The event is complete",
      };
      return labels[this.state.stage] || "";
    },

    get stageDescription() {
      const labels = {
        submission:
          "Each guest can add up to five names. Other guests cannot see them until voting starts; stage 1 opens once the pool reaches the minimum.",
        stage1:
          "Names are shown without their reasons. Select up to 10 names to send to the next stage.",
        stage2:
          "The top 10 names are shown with their reasons. Select up to 3 names for the final.",
        final: "The top 3 names are shown with their reasons. Select one name to win.",
        tiebreak:
          "The finalists are tied. Select one name. If it ties again, the moderator will rerun this round.",
        closed: "The winner and the published results are below.",
      };
      return labels[this.state.stage] || "";
    },

    get isVotingStage() {
      return ["stage1", "stage2", "final", "tiebreak"].includes(
        this.state.stage,
      );
    },

    get submissionShortfall() {
      return Math.max(
        (this.state.min_suggestions || 0) - (this.state.total_suggestions || 0),
        0,
      );
    },

    get voteHeading() {
      const labels = {
        stage1: "Stage 1 ballot",
        stage2: "Stage 2 ballot",
        final: "Final ballot",
        tiebreak: "Tiebreak ballot",
      };
      return labels[this.state.stage] || "Ballot";
    },

    get advanceLabel() {
      const labels = {
        submission: "Start stage 1",
        stage1: "Close stage 1 and open stage 2",
        stage2: "Close stage 2 and open the final",
        final: "Close the final",
        tiebreak: "Close the tiebreak",
        closed: "Event closed",
      };
      return labels[this.state.stage] || "";
    },

    stageResultLabel(stage) {
      const labels = {
        stage1: "Stage 1 results (names only)",
        stage2: "Stage 2 results (top 10)",
        final: "Final results",
        tiebreak: "Tiebreak results",
      };
      return labels[stage] || stage;
    },
  };
}
