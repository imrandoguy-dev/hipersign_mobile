// Calls per caller per day: total, connected, New vs Follow-up, talk time, new leads.
frappe.query_reports["Calling Activity"] = {
	filters: [
		{
			fieldname: "from_date",
			label: __("From"),
			fieldtype: "Date",
			default: frappe.datetime.add_days(frappe.datetime.get_today(), -6),
			reqd: 1,
		},
		{
			fieldname: "to_date",
			label: __("To"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "caller",
			label: __("Caller"),
			fieldtype: "Link",
			options: "User",
		},
		{
			fieldname: "group_by",
			label: __("Show"),
			fieldtype: "Select",
			options: ["Day and Caller", "Caller", "Day"],
			default: "Day and Caller",
		},
	],
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (column.fieldname === "connect_rate" && data && data.connect_rate !== undefined) {
			const color = data.connect_rate >= 50 ? "green" : data.connect_rate >= 30 ? "orange" : "red";
			value = `<span style="color:var(--${color}-600, ${color})">${value}</span>`;
		}
		return value;
	},
};
