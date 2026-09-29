/* Last script on the page: everything else has defined itself by now. */
window.addEventListener("DOMContentLoaded", () => {
  App.shell.init().catch((err) => {
    console.error(err);
    document.getElementById("grid-end").replaceChildren(
      App.el("div", { class: "fatal" },
        App.el("h2", { text: "meerpic could not start" }),
        App.el("p", { text: err.message }),
        App.el("p", { class: "muted small", text: "The database may still be coming up; reloading in a moment usually fixes it." }),
      ),
    );
  });
});
