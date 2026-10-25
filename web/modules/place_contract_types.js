/** Dependency-free Places JSDoc mirror of ouroboros.gateway.client_contracts.
 *  Re-exported by client_contract_types.js; one PlaceSummary for every Places surface. */

/**
 * Connect Silent; ids default to empty. An instance label is prefixed with the service.
 * @typedef {Object} PlaceConnectRequest
 * @property {string} connector
 * @property {string=} account_id
 * @property {string=} conversation_id
 * @property {string=} label
 */

/**
 * Sanitized host mark or SVG geometry; an unavailable mark is {}.
 * @typedef {Object} PlaceIcon
 * @property {'host'|'svg'=} kind
 * @property {string=} name
 * @property {string=} svg
 */

/**
 * @typedef {Object} PlaceReadiness
 * @property {string} text
 * @property {string} tone
 */

/**
 * One projection for list, write/conflict responses and the model's place map.
 * @typedef {Object} PlaceSummary
 * @property {string} place_id
 * @property {string} connector
 * @property {string} provider
 * @property {string} account_id
 * @property {string} conversation_id
 * @property {string} label
 * @property {'active'|'silent'|'paused'} state
 * @property {Array<Object<string,string>>} wakes
 * @property {Array<Object<string,string>>} notes
 * @property {Array<Object<string,string>>} owner_accounts
 * @property {string} working_folder
 * @property {string} model
 * @property {Array<string>} context_topics
 * @property {Array<string>} legacy_binding_ids
 * @property {number} revision
 * @property {string} thread_id
 * @property {string} destination
 * @property {string} scope
 * @property {string} contribution_id
 * @property {Object<string,string>|null} reply_destination
 * @property {PlaceIcon} icon
 * @property {PlaceReadiness} readiness
 * @property {Array<string>} wake_suggestions
 * @property {Array<*>=} facts
 * @property {Object<string,*>=} source
 * @property {string=} settings
 * @property {Array<Object>|null} rooms
 * @property {number|null} unanswered Open focus turns, not owed replies.
 * @property {string} last_activity_at
 * @property {string} activity_coverage
 * @property {number|null} spent_24h_usd
 * @property {Array<Object>} account_candidates
 */

/**
 * @typedef {Object} PlaceDisconnectRequest
 * @property {number=} expected_revision
 */

/**
 * Confirm a person; omitted account_id uses the place's account.
 * @typedef {Object} PlaceOwnerAccountRequest
 * @property {string} platform_actor_id
 * @property {string=} label
 * @property {string=} account_id
 */

/**
 * Atomic owner choices. Send a nonempty body; expected_revision compares before writing.
 * @typedef {Object} PlaceUpdateRequest
 * @property {'active'|'silent'|'paused'=} state
 * @property {Array<string>=} wakes
 * @property {string=} add_note
 * @property {string=} remove_note
 * @property {string=} working_folder
 * @property {string=} model
 * @property {PlaceOwnerAccountRequest=} confirm_owner_account
 * @property {string=} revoke_owner_account
 * @property {number=} expected_revision
 */

/**
 * A captured local conversation, not a complete remote-provider history.
 * @typedef {Object} PlaceRoomSummary
 * @property {number} room_chat_id
 * @property {number|null} unanswered_count
 * @property {string} address
 * @property {string|null} label
 * @property {string} last_at  // latest captured arrival
 * @property {string} conversation_id
 * @property {string} thread_id
 */

/**
 * Store-wide chat import/gaps, not provider completeness.
 * @typedef {Object} PlaceRoomsCoverage
 * @property {'arrivals'} discovery
 * @property {boolean} importing
 * @property {boolean} gaps
 */

/**
 * @typedef {Object} PlaceRoomsResponse
 * @property {string} place_id
 * @property {Array<PlaceRoomSummary>} rooms
 * @property {PlaceRoomsCoverage} coverage
 */
